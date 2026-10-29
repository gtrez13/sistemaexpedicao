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
