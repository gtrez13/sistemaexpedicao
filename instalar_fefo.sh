#!/usr/bin/env bash
# Instalador do FEFO na bipagem (rode DENTRO da pasta do backend, onde esta o app.py)
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: o FEFO e SO da LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

cat > fefo_routes.py <<'FEFOEOF'
"""
FEFO na bipagem (backend).

O operador bipa o SKU; se o SKU tem lotes com saldo, o WMS manda pegar o lote de
validade mais próxima e só libera o bip quando a etiqueta do lote bipada for esse lote
(ou outro com a MESMA validade). Lote mais novo com lote mais antigo em estoque => bloqueia.

Fonte dos dados: cópia local (tabelas fefo_*) abastecida por fefo_sync.py, que lê o Bling
devagar (lotes + saldo por lote). NENHUM bip chama o Bling.

Endpoints (registrar com url_prefix="/api"):
    GET  /api/fefo/sku/<sku>   -> {exigir, proximo, lotes[]}      (só banco)
    POST /api/fefo/validar     -> {sku, lote, ml_id?, qtd?}       (só banco)
    POST /api/fefo/desfazer    -> {sku, lote}  estorna o último bip FEFO
    GET  /api/fefo/status      -> saúde da cópia local

Segurança de rollout:
  * Desligado por padrão: só exige lote se FEFO_ATIVO=1 no ambiente do serviço;
    FEFO_SKUS=A,B limita a poucos SKUs (modo teste em produção).
  * SKU sem lote com saldo na cópia local => exigir:false (bipa como hoje).
  * O frontend, se o FEFO falhar/oscilar, segue bipando como hoje (fail-open).

Limite conhecido: o saldo do Bling só baixa do lote quando a NF sai com lote (próxima
pendência). Até lá, saldo efetivo = saldo do Bling - TODOS os bips FEFO (acumulado, não zera na sync).
Quando o passo de lote na NF existir, ele deve abater/zerar esse consumo local.
"""
from __future__ import annotations

import os
import re
import time

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required   # mesma sessão do resto do WMS (/api/auth/me)

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

fefo_routes = Blueprint("fefo_routes", __name__)
_tabelas_ok = False


def ativo(sku=None) -> bool:
    """FEFO_ATIVO=1 liga. FEFO_SKUS=A,B restringe a esses SKUs (use para testar em produção
    sem afetar os demais). Sem FEFO_ATIVO nada é exigido."""
    if os.environ.get("FEFO_ATIVO", "0").strip() not in ("1", "true", "True", "sim"):
        return False
    lista = {x.strip().lower() for x in os.environ.get("FEFO_SKUS", "").split(",") if x.strip()}
    return not lista or sku_key(sku) in lista


# ───────────────────────── banco ─────────────────────────
def garantir_tabelas(conn=None):
    global _tabelas_ok
    if _tabelas_ok:
        return

    def _run(c):
        c.execute("""
            CREATE TABLE IF NOT EXISTS fefo_sku (
                sku_key   TEXT PRIMARY KEY,
                sku       TEXT NOT NULL,
                id_bling  BIGINT,
                n_lotes   INTEGER NOT NULL DEFAULT 0,
                sync_em   BIGINT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS fefo_lote (
                sku_key   TEXT NOT NULL,
                id_lote   BIGINT NOT NULL,
                lote      TEXT NOT NULL,
                lote_key  TEXT NOT NULL,
                validade  TEXT,
                saldo     DOUBLE PRECISION NOT NULL DEFAULT 0,
                sync_em   BIGINT NOT NULL,
                PRIMARY KEY (sku_key, id_lote)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_fefo_lote_sku ON fefo_lote (sku_key)")
        pk = "BIGSERIAL PRIMARY KEY" if _eh_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS fefo_bip (
                id        {pk},
                sku_key   TEXT NOT NULL,
                lote_key  TEXT NOT NULL,
                ml_id     TEXT,
                qtd       INTEGER NOT NULL DEFAULT 1,
                criado_em BIGINT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_fefo_bip_sku ON fefo_bip (sku_key, lote_key)")
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS fefo_excecao (
                id        {pk},
                sku_key   TEXT NOT NULL,
                sku       TEXT NOT NULL,
                lote_lido TEXT,
                motivo    TEXT,
                ml_id     TEXT,
                usuario   TEXT,
                criado_em BIGINT NOT NULL
            )
        """)

    if conn is not None:
        _run(conn)
    else:
        with db_session() as c:
            _run(c)
    _tabelas_ok = True


def _eh_postgres() -> bool:
    """Mesma regra do db/__init__.py: DATABASE_URL definido => Postgres."""
    return bool(os.environ.get("DATABASE_URL"))


def pode_liberar() -> bool:
    """Quem pode liberar o bip sem conferir o lote (fica registrado). FEFO_LIBERAR_ROLES, padrão admin,supervisor."""
    roles = {r.strip().lower() for r in os.environ.get("FEFO_LIBERAR_ROLES", "admin,supervisor").split(",") if r.strip()}
    return str(getattr(current_user, "role", "") or "").lower() in roles


def sku_key(sku) -> str:
    return (sku or "").strip()[:64].lower()


def lote_key(txt) -> str:
    return re.sub(r"[^A-Z0-9]", "", (txt or "").upper())


# ───────────────────────── regra FEFO ─────────────────────────
def _prefixos_ignorados():
    """Lotes 'INI-…' são o lote inicial criado pela carga ANVISA, não o lote impresso na caixa:
    ninguém consegue bipar essa etiqueta, então não entram na regra. Ajuste em FEFO_IGNORAR_PREFIXOS
    (lista separada por vírgula; vazio = não ignora nada)."""
    return tuple(p.strip().upper() for p in os.environ.get("FEFO_IGNORAR_PREFIXOS", "INI-").split(",") if p.strip())


def lotes_efetivos(conn, key: str):
    """Lotes do SKU com saldo efetivo > 0, do mais antigo (validade) para o mais novo."""
    rows = conn.execute(
        "SELECT id_lote, lote, lote_key, validade, saldo, sync_em FROM fefo_lote WHERE sku_key=?",
        (key,)).fetchall()
    out = []
    ignorar = _prefixos_ignorados()
    for r in rows:
        if ignorar and (r["lote"] or "").upper().startswith(ignorar):
            continue
        # Consumo ACUMULADO: o saldo do lote no Bling só baixa quando a NF sair com lote (passo futuro).
        # Até lá o consumo FEFO fica registrado aqui e NÃO zera na sincronização — senão um lote já
        # esgotado voltaria a aparecer com saldo a cada sync e travaria o operador.
        usados = conn.execute(
            "SELECT COALESCE(SUM(qtd),0) AS q FROM fefo_bip WHERE sku_key=? AND lote_key=?",
            (key, r["lote_key"])).fetchone()["q"]
        efetivo = float(r["saldo"]) - float(usados or 0)
        if efetivo > 0:
            out.append({"lote": r["lote"], "lote_key": r["lote_key"],
                        "validade": r["validade"] or None, "saldo": efetivo})
    out.sort(key=lambda x: (x["validade"] is None, x["validade"] or "", x["lote_key"]))
    return out


def candidatos_lote(raw: str):
    """Possíveis lotes dentro do que o leitor entregou (texto puro ou GS1)."""
    raw = (raw or "").strip()
    c = {lote_key(raw)}
    m = re.search(r"\(10\)([^()]+)", raw)                    # GS1 com parênteses
    if m:
        c.add(lote_key(m.group(1)))
    if raw.startswith("01") and len(raw) > 18 and raw[16:18] == "10":   # GS1 sem parênteses
        c.add(lote_key(re.split(r"[\x1d]", raw[18:])[0]))
    c.discard("")
    return c


def casa_lote(raw: str, lotes):
    cands = candidatos_lote(raw)
    norm = lote_key(raw)
    for l in lotes:
        if l["lote_key"] in cands:
            return l
    # etiqueta com texto extra: vale a chave MAIS LONGA contida (L-VELHO2 é prefixo de L-VELHO-26)
    achados = [l for l in lotes if len(l["lote_key"]) >= 4 and l["lote_key"] in norm]
    return max(achados, key=lambda l: len(l["lote_key"])) if achados else None


# ───────────────────────── endpoints ─────────────────────────
@fefo_routes.get("/fefo/sku/<path:sku>")
@login_required
def fefo_sku(sku):
    garantir_tabelas()
    if not ativo(sku):
        return jsonify({"exigir": False, "motivo": "desligado"})
    key = sku_key(sku)
    with db_session() as conn:
        lotes = lotes_efetivos(conn, key)
    if not lotes:
        return jsonify({"exigir": False, "motivo": "sem_lote"})
    pr = lotes[0]
    return jsonify({
        "exigir": True, "pode_liberar": pode_liberar(),
        "proximo": {"lote": pr["lote"], "validade": pr["validade"], "saldo": pr["saldo"]},
        "lotes": [{"lote": l["lote"], "validade": l["validade"], "saldo": l["saldo"]} for l in lotes],
    })


@fefo_routes.post("/fefo/validar")
@login_required
def fefo_validar():
    d = request.get_json(silent=True) or {}
    sku = (d.get("sku") or "").strip()
    lido = (d.get("lote") or "").strip()
    ml_id = (d.get("ml_id") or "").strip() or None
    try:
        qtd = max(int(d.get("qtd") or 1), 1)
    except (TypeError, ValueError):
        qtd = 1
    if not sku or not lido:
        return jsonify({"error": "sku e lote são obrigatórios"}), 400

    garantir_tabelas()
    if not ativo(sku):
        return jsonify({"ok": True, "exigir": False})
    key = sku_key(sku)
    with db_session() as conn:
        lotes = lotes_efetivos(conn, key)
        if not lotes:
            return jsonify({"ok": True, "exigir": False})
        pr = lotes[0]
        escolhido = casa_lote(lido, lotes)
        if escolhido is None:
            # lote lido não está entre os lotes com saldo: esgotado ou desconhecido
            cands = sorted(candidatos_lote(lido)) or [""]
            conhecido = conn.execute(
                "SELECT 1 FROM fefo_lote WHERE sku_key=? AND lote_key IN (%s) LIMIT 1" % ",".join("?" * len(cands)),
                (key, *cands)).fetchone()
            motivo = "lote_sem_saldo" if conhecido else "lote_desconhecido"
            msg = ("Esse lote está sem saldo no sistema." if motivo == "lote_sem_saldo"
                   else "Lote não consta no sistema para este SKU. Confira a etiqueta.")
            return jsonify({"ok": False, "motivo": motivo, "mensagem": msg, "pode_liberar": pode_liberar(),
                            "deve_pegar": {"lote": pr["lote"], "validade": pr["validade"], "saldo": pr["saldo"]}})
        # aceita o mais antigo ou qualquer lote com a MESMA validade do mais antigo
        if escolhido["validade"] != pr["validade"]:
            return jsonify({
                "ok": False, "motivo": "lote_mais_novo", "pode_liberar": pode_liberar(),
                "mensagem": f"Há lote mais antigo em estoque: {pr['lote']} (val {pr['validade'] or 's/ validade'}). Pegue esse primeiro.",
                "deve_pegar": {"lote": pr["lote"], "validade": pr["validade"], "saldo": pr["saldo"]},
            })
        conn.execute(
            "INSERT INTO fefo_bip (sku_key, lote_key, ml_id, qtd, criado_em) VALUES (?,?,?,?,?)",
            (key, escolhido["lote_key"], ml_id, qtd, int(time.time())))
    return jsonify({"ok": True, "exigir": True,
                    "lote": escolhido["lote"], "validade": escolhido["validade"]})


@fefo_routes.post("/fefo/liberar")
@login_required
def fefo_liberar():
    """Supervisor/admin libera o bip sem conferir o lote. Fica registrado (base do 'não achado em estoque')."""
    if not pode_liberar():
        return jsonify({"error": "Somente supervisor/admin pode liberar sem conferir o lote."}), 403
    d = request.get_json(silent=True) or {}
    sku = (d.get("sku") or "").strip()
    if not sku:
        return jsonify({"error": "sku é obrigatório"}), 400
    garantir_tabelas()
    quem = str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")
    with db_session() as conn:
        conn.execute(
            "INSERT INTO fefo_excecao (sku_key, sku, lote_lido, motivo, ml_id, usuario, criado_em) VALUES (?,?,?,?,?,?,?)",
            (sku_key(sku), sku[:64], (d.get("lote_lido") or "")[:120], (d.get("motivo") or "")[:200],
             (d.get("ml_id") or "")[:64] or None, quem[:80], int(time.time())))
    return jsonify({"success": True})


@fefo_routes.get("/fefo/excecoes")
@login_required
def fefo_excecoes():
    if not pode_liberar():
        return jsonify({"error": "Somente supervisor/admin."}), 403
    garantir_tabelas()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT sku, lote_lido, motivo, ml_id, usuario, criado_em FROM fefo_excecao ORDER BY id DESC LIMIT 200").fetchall()
    return jsonify({"excecoes": [dict(r) for r in rows]})


@fefo_routes.post("/fefo/desfazer")
@login_required
def fefo_desfazer():
    """Estorna o último bip FEFO do SKU+lote (o bip normal falhou depois da liberação)."""
    d = request.get_json(silent=True) or {}
    sku = (d.get("sku") or "").strip()
    lk = lote_key(d.get("lote"))
    if not sku or not lk:
        return jsonify({"error": "sku e lote são obrigatórios"}), 400
    garantir_tabelas()
    with db_session() as conn:
        conn.execute(
            "DELETE FROM fefo_bip WHERE id = (SELECT MAX(id) FROM fefo_bip WHERE sku_key=? AND lote_key=?)",
            (sku_key(sku), lk))
    return jsonify({"success": True})


@fefo_routes.get("/fefo/status")
@login_required
def fefo_status():
    garantir_tabelas()
    with db_session() as conn:
        s = conn.execute("SELECT COUNT(*) AS n, MAX(sync_em) AS ult FROM fefo_sku").fetchone()
        l = conn.execute("SELECT COUNT(*) AS n FROM fefo_lote").fetchone()
        c = conn.execute("SELECT COUNT(*) AS n FROM fefo_sku WHERE n_lotes>0").fetchone()
    ult = s["ult"]
    return jsonify({
        "fefo_ativo": ativo(),
        "fefo_skus": os.environ.get("FEFO_SKUS", "") or None,
        "skus_sincronizados": s["n"], "skus_com_lote": c["n"], "lotes": l["n"],
        "ultima_sync_ha_s": (int(time.time()) - int(ult)) if ult else None,
    })
FEFOEOF

cat > fefo_sync.py <<'SYNCEOF'
#!/usr/bin/env python3
"""
Sincroniza lotes + saldo por lote do Bling para a cópia local do WMS (tabelas fefo_*).

Rode DENTRO da pasta do backend, com o venv do WMS:
    python fefo_sync.py --pendentes            # SKUs de pedidos ainda não concluídos
    python fefo_sync.py --todos --max 300      # todos os SKUs do cadastro (repita até 0 SKU(s))
    python fefo_sync.py --skus MP001,V000023   # SKUs específicos
    python fefo_sync.py --arquivo skus.txt     # um SKU por linha
    python fefo_sync.py --pendentes --max 80   # limite por rodada (padrão 150)

Cuidados com 429 (leitura apenas — este script NUNCA escreve no Bling):
  * nunca renova token: lê o token que o cron já mantém; vencido => encerra
  * >= 0.6 s entre chamadas; timeout curto; ~3 chamadas por SKU na 1ª vez, 2 depois
  * ao primeiro 429/401/403 PARA (mostra Retry-After) e retoma na próxima rodada
  * SKU sincronizado há pouco é pulado (--horas, padrão 6; sem lote: 24 h)
  * processa primeiro quem está há mais tempo sem sincronizar
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

try:                       # mesmo ajuste de JWT (enable-jwt) que o WMS usa; o cabeçalho abaixo cobre se faltar
    import bling_jwt  # noqa: F401
except Exception:
    pass

from db import db_session
from fefo_routes import garantir_tabelas, lote_key, sku_key
from services.bling_service import BlingService

BASE = "https://api.bling.com.br/Api/v3"
PAUSA = 0.6
LIMITE_PAGINA = 100
TIMEOUT = (3, 10)


class Parar(Exception):
    pass


class Bling:
    def __init__(self):
        self.bs = BlingService()
        self.ultima = 0.0
        self.dep = None

    def get(self, path, params=None):
        t = self.bs._load_tokens() or {}
        try:
            exp = float(t.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0.0
        if not t.get("access_token") or time.time() > exp - 120:
            raise Parar("token vencido — espere o cron renovar (renovar_bling_tokens.sh) e rode de novo")
        espera = self.ultima + PAUSA - time.time()
        if espera > 0:
            time.sleep(espera)
        self.ultima = time.time()
        try:
            r = requests.get(BASE + path, params=params, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {t['access_token']}", "Accept": "application/json",
                                     "enable-jwt": "1"})
        except requests.RequestException as e:
            raise Parar(f"rede: {e}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            raise Parar(f"429 do Bling (Retry-After {r.headers.get('Retry-After', '?')}s) — parei de propósito")
        if r.status_code in (401, 403):
            raise Parar(f"{r.status_code} do Bling — token/permissão; não insisto")
        if r.status_code >= 500:
            raise Parar(f"{r.status_code} do Bling")
        return None

    def deposito(self):
        if self.dep:
            return self.dep
        env = os.environ.get("BLING_DEPOSITO_ID", "").strip()
        if env:
            self.dep = int(env)
            return self.dep
        r = self.get("/depositos", {"limite": 100}) or {}
        deps = r.get("data") or []
        pad = next((d for d in deps if d.get("padrao")), deps[0] if deps else None)
        if not pad:
            raise Parar("não achei depósito (defina BLING_DEPOSITO_ID)")
        self.dep = int(pad["id"])
        print(f"depósito usado: {self.dep} ({pad.get('descricao')})  [fixe com BLING_DEPOSITO_ID]")
        return self.dep


def skus_todos(conn):
    rows = conn.execute("SELECT DISTINCT sku FROM produtos WHERE sku IS NOT NULL AND sku <> ''").fetchall()
    return [r["sku"] for r in rows if r["sku"]]


def skus_pendentes(conn):
    rows = conn.execute("""
        SELECT DISTINCT pr.sku AS sku
        FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        JOIN pedidos p ON p.id = pp.pedido_id
        WHERE p.status <> 'CONCLUIDO'
    """).fetchall()
    return [r["sku"] for r in rows if r["sku"]]


def ordenar_e_filtrar(conn, skus, horas, forcar):
    agora = int(time.time())
    out = []
    for s in dict.fromkeys(x.strip() for x in skus if x and x.strip()):
        r = conn.execute("SELECT id_bling, n_lotes, sync_em FROM fefo_sku WHERE sku_key=?", (sku_key(s),)).fetchone()
        if r and not forcar:
            idade = agora - int(r["sync_em"])
            if idade < (horas * 3600 if r["n_lotes"] else max(horas, 24) * 3600):
                continue
        out.append((int(r["sync_em"]) if r else 0, s, r["id_bling"] if r else None))
    out.sort()
    return [(s, i) for _, s, i in out]


def sincronizar_sku(b: Bling, sku: str, id_bling):
    if not id_bling:
        r = b.get("/produtos", {"codigo": sku, "limite": 5}) or {}
        data = r.get("data") or []
        prod = next((p for p in data if str(p.get("codigo", "")).lower() == sku.lower()), None)
        id_bling = prod.get("id") if prod else None
        if not id_bling:
            return None, []                       # produto não existe no Bling
    r = b.get("/produtos/lotes", {"idsProdutos[]": id_bling}) or {}
    lotes = r.get("data") or []
    if not lotes:
        return id_bling, []
    if len(lotes) >= LIMITE_PAGINA:
        # uma página cheia pode estar CORTADA: lista incompleta levaria o FEFO a mandar pegar o lote errado
        print(f"  !! {sku}: {len(lotes)} lotes (limite da página) — lista pode estar incompleta; FEFO ignora este SKU")
        return id_bling, []
    ids = [l["idLote"] for l in lotes if l.get("idLote") is not None]
    r = b.get(f"/produtos/{id_bling}/lotes/depositos/{b.deposito()}/saldo", {"idsLotes[]": ids}) or {}
    saldos = {it["idLote"]: float(it.get("saldoAtual") or 0)
              for it in (r.get("data") or []) if it.get("idLote") is not None}
    return id_bling, [{
        "id_lote": l["idLote"], "lote": l.get("codigoLote") or "", "validade": (l.get("dataValidade") or "")[:10] or None,
        "saldo": saldos.get(l["idLote"], 0.0)} for l in lotes if l.get("idLote") is not None and l.get("codigoLote")]


def gravar(conn, sku, id_bling, lotes):
    agora = int(time.time())
    key = sku_key(sku)
    conn.execute("DELETE FROM fefo_lote WHERE sku_key=?", (key,))
    for l in lotes:
        conn.execute(
            "INSERT INTO fefo_lote (sku_key, id_lote, lote, lote_key, validade, saldo, sync_em) VALUES (?,?,?,?,?,?,?)",
            (key, l["id_lote"], l["lote"], lote_key(l["lote"]), l["validade"], l["saldo"], agora))
    conn.execute("""
        INSERT INTO fefo_sku (sku_key, sku, id_bling, n_lotes, sync_em) VALUES (?,?,?,?,?)
        ON CONFLICT (sku_key) DO UPDATE SET sku=EXCLUDED.sku, id_bling=EXCLUDED.id_bling,
            n_lotes=EXCLUDED.n_lotes, sync_em=EXCLUDED.sync_em
    """, (key, sku, id_bling, len(lotes), agora))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus")
    ap.add_argument("--arquivo")
    ap.add_argument("--pendentes", action="store_true")
    ap.add_argument("--todos", action="store_true", help="todos os SKUs do cadastro do WMS (rode em lotes: --max)")
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--horas", type=float, default=6)
    ap.add_argument("--forcar", action="store_true")
    a = ap.parse_args()

    garantir_tabelas()
    skus = []
    if a.skus:
        skus += a.skus.split(",")
    if a.arquivo:
        skus += open(a.arquivo, encoding="utf-8").read().split()
    if a.pendentes or a.todos:
        try:
            with db_session() as conn:
                skus += skus_todos(conn) if a.todos else skus_pendentes(conn)
        except Exception as e:
            print(f"não consegui listar SKUs pendentes ({e}). Use --skus ou --arquivo.")
            return 2
    if not skus:
        print("nenhum SKU informado (use --pendentes, --skus ou --arquivo)")
        return 2

    with db_session() as conn:
        fila = ordenar_e_filtrar(conn, skus, a.horas, a.forcar)[: a.max]
    print(f"{len(fila)} SKU(s) para sincronizar")
    b = Bling()
    ok = com_lote = 0
    try:
        for sku, id_bling in fila:
            idb, lotes = sincronizar_sku(b, sku, id_bling)
            with db_session() as conn:
                gravar(conn, sku, idb, lotes)
            ok += 1
            com_lote += 1 if lotes else 0
            print(f"  {sku}: {len(lotes)} lote(s)" + (f", saldo {sum(l['saldo'] for l in lotes):g}" if lotes else ""))
    except Parar as e:
        print(f"PAREI: {e}")
        print(f"feitos nesta rodada: {ok} ({com_lote} com lote)")
        return 1
    print(f"ok: {ok} SKU(s), {com_lote} com lote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
SYNCEOF
chmod +x fefo_sync.py

# registra o blueprint no app.py (idempotente, com backup)
python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "fefo_routes" in s:
    print(">> app.py ja tinha o fefo_routes (ok, nada a fazer)")
else:
    linha = '    from fefo_routes import fefo_routes as fefo_bp\n    app.register_blueprint(fefo_bp, url_prefix="/api")\n'
    ancoras = ['    app.register_blueprint(mapa_bp, url_prefix="/api")\n',
               "    app.register_blueprint(api_routes)\n",
               "    app.register_blueprint(main_routes)\n"]
    for a in ancoras:
        if a in s:
            open(f + ".bak_fefo", "w", encoding="utf-8").write(s)
            open(f, "w", encoding="utf-8").write(s.replace(a, a + linha, 1))
            print(">> app.py patchado (backup em app.py.bak_fefo): /api/fefo/* registrado")
            break
    else:
        print("!! Nao achei onde registrar. Adicione dentro de create_app():"); print(linha); raise SystemExit(1)
PYEOF

echo ""
echo "OK. FEFO fica DESLIGADO ate voce definir FEFO_ATIVO=1 no servico. Reinicie o backend e teste:"
echo "  curl -s localhost:PORTA/api/fefo/status"
