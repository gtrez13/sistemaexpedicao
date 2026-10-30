#!/usr/bin/env bash
# Instalador do "Nao achado em estoque" (rode DENTRO da pasta do backend, onde esta o app.py)
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

cat > naoachado_routes.py <<'NAEOF'
"""
"Não achado em estoque" (backend).

Fluxo: o operador procura o produto (mapa do galpão mostra onde fica), não acha e registra.
O SUPERVISOR de logística tem NAO_ACHADO_HORAS (padrão 48) para EXPLICAR o motivo; depois o ADMIN
dá o parecer (aceitar ou cobrar). Ocorrência sem explicação no prazo fica "vencida" (cobrável).

Endpoints (url_prefix="/api"; todos exigem login):
    POST /api/nao-achado                 operador/qualquer logado: {sku, nome?, qtd?, ml_id?, estoque_bling?, locais?, obs?}
    GET  /api/nao-achado[?status=]       supervisor/admin: lista (aberto|vencido|explicado|aceito|cobrar|todos)
    POST /api/nao-achado/<id>/explicar   supervisor/admin: {texto} (mín. 10 caracteres)
    POST /api/nao-achado/<id>/decidir    admin: {decisao: aceito|cobrar, parecer?, confirmar_sem_estoque?}
    POST /api/nao-achado/<id>/reprocessar admin: refaz o que falhou (zerar no Bling / avisar o Velaplace)
    GET  /api/nao-achado/resumo          supervisor/admin: contadores (abertos, vencidos, a decidir)

ÚLTIMA ETAPA (admin com confirmar_sem_estoque=true): o WMS zera o saldo do produto NO DEPÓSITO da
LHVMed (BLING_DEPOSITO_ID) no Bling (lançamento de balanço = 0), confere o saldo depois e avisa o
Velaplace (VELAPLACE_API_URL + VELAPLACE_TOKEN; sem isso o aviso fica "simulado").
Só roda com NAO_ACHADO_ZERAR_BLING=1 no ambiente do serviço (senão fica "desligado" e nada é escrito no Bling).
Falha nunca derruba a decisão: o resultado fica gravado e o admin pode "reprocessar".
"""
from __future__ import annotations

import os
import time

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

naoachado_routes = Blueprint("naoachado_routes", __name__)
_tabelas_ok = False


def horas_prazo() -> float:
    try:
        return float(os.environ.get("NAO_ACHADO_HORAS", "48"))
    except ValueError:
        return 48.0


def _role() -> str:
    return str(getattr(current_user, "role", "") or "").lower()


def _quem() -> str:
    return str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")[:80]


def eh_gestor() -> bool:
    return _role() in ("admin", "supervisor")


def eh_admin() -> bool:
    return _role() == "admin"


def garantir_tabelas(conn=None):
    global _tabelas_ok
    if _tabelas_ok:
        return

    def _run(c):
        pk = "BIGSERIAL PRIMARY KEY" if os.environ.get("DATABASE_URL") else "INTEGER PRIMARY KEY AUTOINCREMENT"
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS nao_achado (
                id            {pk},
                sku           TEXT NOT NULL,
                sku_key       TEXT NOT NULL,
                nome          TEXT,
                qtd           DOUBLE PRECISION NOT NULL DEFAULT 1,
                ml_id         TEXT,
                estoque_bling DOUBLE PRECISION,
                locais        TEXT,
                obs           TEXT,
                reportado_por TEXT,
                reportado_em  BIGINT NOT NULL,
                prazo_em      BIGINT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'aberto',
                explicacao    TEXT,
                explicado_por TEXT,
                explicado_em  BIGINT,
                parecer       TEXT,
                decidido_por  TEXT,
                decidido_em   BIGINT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_nao_achado_status ON nao_achado (status, reportado_em)")
        extras = [("estoque_status", "TEXT"), ("estoque_anterior", "DOUBLE PRECISION"), ("estoque_msg", "TEXT"),
                  ("estoque_zerado_em", "BIGINT"), ("velaplace_status", "TEXT"), ("velaplace_msg", "TEXT")]
        if os.environ.get("DATABASE_URL"):
            for col, tp in extras:
                c.execute(f"ALTER TABLE nao_achado ADD COLUMN IF NOT EXISTS {col} {tp}")
        else:
            have = {r[1] for r in c.execute("PRAGMA table_info(nao_achado)").fetchall()}
            for col, tp in extras:
                if col not in have:
                    c.execute(f"ALTER TABLE nao_achado ADD COLUMN {col} {tp}")

    if conn is not None:
        _run(conn)
    else:
        with db_session() as c:
            _run(c)
    _tabelas_ok = True


# ───────────────────── zerar no Bling + avisar o Velaplace ─────────────────────
def _zerar_ligado() -> bool:
    return os.environ.get("NAO_ACHADO_ZERAR_BLING", "0").strip() in ("1", "true", "True", "sim")


def _saldo_no_deposito(bs, produto_id, deposito):
    """Saldo físico do produto SÓ no depósito informado (None se não deu para ler)."""
    r = bs._get("/estoques/saldos", params=[("idsProdutos[]", str(int(produto_id)))])
    if r.status_code != 200:
        raise RuntimeError(f"Bling saldos HTTP {r.status_code}")
    rows = (r.json() or {}).get("data") or []
    for row in rows:
        for d in (row.get("depositos") or []):
            if str(d.get("id")) == str(deposito):
                return float(d.get("saldoFisico") or 0)
    return 0.0                      # depósito sem linha nos saldos = sem saldo ali


def zerar_no_bling(sku: str, motivo: str):
    """-> (status, saldo_anterior, mensagem). status: ok | ja_zerado | desligado | erro."""
    if not _zerar_ligado():
        return "desligado", None, "NAO_ACHADO_ZERAR_BLING não está ligado: nada foi escrito no Bling."
    dep = os.environ.get("BLING_DEPOSITO_ID", "").strip()
    if not dep:
        return "erro", None, "BLING_DEPOSITO_ID não definido: não sei qual depósito zerar."
    try:
        from services.bling_service import BlingService
        bs = BlingService()
        r = bs._get("/produtos", params={"codigo": sku, "limite": 5})
        if r.status_code != 200:
            return "erro", None, f"Bling produtos HTTP {r.status_code}"
        prods = [p for p in ((r.json() or {}).get("data") or []) if str(p.get("codigo", "")).lower() == sku.lower()]
        if not prods:
            return "erro", None, "Produto não encontrado no Bling pelo código (SKU)."
        pid = int(prods[0]["id"])
        antes = _saldo_no_deposito(bs, pid, dep)
        if antes == 0:
            return "ja_zerado", 0.0, "O saldo já era 0 no depósito da LHVMed."
        corpo = {"produto": {"id": pid}, "deposito": {"id": int(dep)}, "operacao": "B", "quantidade": 0,
                 "observacoes": ("WMS: não achado em estoque. " + (motivo or ""))[:250]}
        w = bs._post("/estoques", json=corpo)
        if w.status_code not in (200, 201):
            return "erro", antes, f"Bling recusou o balanço: HTTP {w.status_code} {w.text[:200]}"
        depois = _saldo_no_deposito(bs, pid, dep)
        if depois != 0:
            return "erro", antes, f"Balanço enviado, mas o saldo continua {depois:g} (conferir no Bling)."
        return "ok", antes, f"Saldo {antes:g} -> 0 no depósito {dep}."
    except Exception as e:   # nunca derruba a decisão do admin
        return "erro", None, f"{type(e).__name__}: {e}"[:300]


def avisar_velaplace(dados: dict):
    """-> (status, mensagem). status: ok | simulado | erro. Contrato em INTEGRACAO_VELAPLACE_ESTOQUE_ZERADO.md."""
    base = os.environ.get("VELAPLACE_API_URL", "").rstrip("/")
    token = os.environ.get("VELAPLACE_TOKEN", "")
    path = os.environ.get("VELAPLACE_ESTOQUE_PATH", "/api/estoque/zerado")
    if not base or not token:
        return "simulado", "Velaplace não configurado (VELAPLACE_API_URL/VELAPLACE_TOKEN): aviso apenas registrado."
    try:
        import requests
        r = requests.post(base + path, json=dados, timeout=8,
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        if r.status_code in (200, 201, 202, 204):
            return "ok", f"Velaplace respondeu {r.status_code}."
        return "erro", f"Velaplace HTTP {r.status_code}: {r.text[:150]}"
    except Exception as e:
        return "erro", f"{type(e).__name__}: {e}"[:300]


def _executar_final(conn, nid, r, parecer):
    """Zera no Bling e avisa o Velaplace; grava o resultado na ocorrência."""
    if r["estoque_status"] in ("ok", "ja_zerado"):       # reprocesso: só o aviso falhou, não zera de novo
        est, antes, msg = r["estoque_status"], r["estoque_anterior"], r["estoque_msg"] or ""
    else:
        est, antes, msg = zerar_no_bling(r["sku"], r["explicacao"] or "")
    agora = int(time.time())
    vst = vmsg = None
    if est in ("ok", "ja_zerado"):
        vst, vmsg = avisar_velaplace({
            "evento": "estoque_zerado", "sku": r["sku"], "produto": r["nome"], "quantidade_anterior": antes,
            "motivo": r["explicacao"] or parecer or "Não achado em estoque (confirmado pelo admin)",
            "ocorrencia_id": nid, "origem": "wms-lhvmed", "confirmado_por": _quem(), "confirmado_em": agora})
    conn.execute(
        "UPDATE nao_achado SET estoque_status=?, estoque_anterior=?, estoque_msg=?, estoque_zerado_em=?, "
        "velaplace_status=?, velaplace_msg=? WHERE id=?",
        (est, antes, msg[:300], (r["estoque_zerado_em"] or agora) if est in ("ok", "ja_zerado") else None, vst, (vmsg or "")[:300] or None, nid))
    return {"estoque_status": est, "estoque_msg": msg, "velaplace_status": vst, "velaplace_msg": vmsg}


def _fmt(r, agora=None):
    agora = agora or int(time.time())
    d = dict(r)
    d["vencido"] = d["status"] == "aberto" and agora > int(d["prazo_em"])
    d["restante_s"] = int(d["prazo_em"]) - agora if d["status"] == "aberto" else None
    d["atrasou"] = bool(d.get("explicado_em")) and int(d["explicado_em"]) > int(d["prazo_em"])
    return d


@naoachado_routes.post("/nao-achado")
@login_required
def criar():
    d = request.get_json(silent=True) or {}
    sku = (d.get("sku") or "").strip()
    if not sku:
        return jsonify({"error": "sku é obrigatório"}), 400
    try:
        qtd = float(d.get("qtd") or 1)
    except (TypeError, ValueError):
        qtd = 1.0
    if qtd <= 0:
        qtd = 1.0
    try:
        est = None if d.get("estoque_bling") is None else float(d.get("estoque_bling"))
    except (TypeError, ValueError):
        est = None
    garantir_tabelas()
    agora = int(time.time())
    key = sku[:64].lower()
    ml = (str(d.get("ml_id") or "")[:64]) or None
    with db_session() as conn:
        # toque duplo / mesma ocorrência ainda aberta do mesmo SKU+pedido: devolve a existente
        ex = conn.execute(
            "SELECT id, prazo_em FROM nao_achado WHERE sku_key=? AND status='aberto' AND COALESCE(ml_id,'')=? "
            "AND reportado_em > ? ORDER BY id DESC LIMIT 1", (key, ml or "", agora - 600)).fetchone()
        if ex:
            return jsonify({"success": True, "id": ex["id"], "prazo_em": ex["prazo_em"], "duplicado": True})
        prazo = agora + int(horas_prazo() * 3600)
        conn.execute(
            "INSERT INTO nao_achado (sku, sku_key, nome, qtd, ml_id, estoque_bling, locais, obs, reportado_por, reportado_em, prazo_em) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sku[:64], key, (d.get("nome") or "")[:200], qtd, ml, est, (d.get("locais") or "")[:200],
             (d.get("obs") or "")[:300], _quem(), agora, prazo))
        nid = conn.execute("SELECT id FROM nao_achado WHERE sku_key=? AND reportado_em=? ORDER BY id DESC LIMIT 1",
                           (key, agora)).fetchone()["id"]
    return jsonify({"success": True, "id": nid, "prazo_em": prazo, "prazo_horas": horas_prazo()})


@naoachado_routes.get("/nao-achado")
@login_required
def listar():
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin."}), 403
    garantir_tabelas()
    st = (request.args.get("status") or "aberto").lower()
    agora = int(time.time())
    sql, args = "SELECT * FROM nao_achado", []
    if st == "vencido":
        sql += " WHERE status='aberto' AND prazo_em < ?"; args.append(agora)
    elif st in ("aberto", "explicado", "aceito", "cobrar"):
        sql += " WHERE status=?"; args.append(st)
    sql += " ORDER BY reportado_em DESC LIMIT 300"
    with db_session() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    return jsonify({"itens": [_fmt(r, agora) for r in rows], "prazo_horas": horas_prazo(),
                    "eh_admin": eh_admin()})


@naoachado_routes.get("/nao-achado/resumo")
@login_required
def resumo():
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin."}), 403
    garantir_tabelas()
    agora = int(time.time())
    with db_session() as conn:
        ab = conn.execute("SELECT COUNT(*) AS n FROM nao_achado WHERE status='aberto'").fetchone()["n"]
        ve = conn.execute("SELECT COUNT(*) AS n FROM nao_achado WHERE status='aberto' AND prazo_em < ?", (agora,)).fetchone()["n"]
        ex = conn.execute("SELECT COUNT(*) AS n FROM nao_achado WHERE status='explicado'").fetchone()["n"]
    return jsonify({"abertos": ab, "vencidos": ve, "a_decidir": ex})


@naoachado_routes.post("/nao-achado/<int:nid>/explicar")
@login_required
def explicar(nid):
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin explica."}), 403
    d = request.get_json(silent=True) or {}
    texto = (d.get("texto") or "").strip()
    if len(texto) < 10:
        return jsonify({"error": "Explique com pelo menos 10 caracteres."}), 400
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT status FROM nao_achado WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Ocorrência não encontrada."}), 404
        if r["status"] != "aberto":
            return jsonify({"error": "Esta ocorrência já foi explicada."}), 409
        conn.execute("UPDATE nao_achado SET status='explicado', explicacao=?, explicado_por=?, explicado_em=? WHERE id=?",
                     (texto[:1000], _quem(), int(time.time()), nid))
    return jsonify({"success": True})


@naoachado_routes.post("/nao-achado/<int:nid>/decidir")
@login_required
def decidir(nid):
    if not eh_admin():
        return jsonify({"error": "Somente admin dá o parecer."}), 403
    d = request.get_json(silent=True) or {}
    dec = (d.get("decisao") or "").lower()
    if dec not in ("aceito", "cobrar"):
        return jsonify({"error": "decisao deve ser 'aceito' ou 'cobrar'"}), 400
    confirmar = bool(d.get("confirmar_sem_estoque"))
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM nao_achado WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Ocorrência não encontrada."}), 404
        if r["status"] in ("aceito", "cobrar"):
            return jsonify({"error": "Já decidida."}), 409
        parecer = (d.get("parecer") or "")[:500]
        conn.execute("UPDATE nao_achado SET status=?, parecer=?, decidido_por=?, decidido_em=? WHERE id=?",
                     (dec, parecer, _quem(), int(time.time()), nid))
        final = {}
        if confirmar:
            final = _executar_final(conn, nid, r, parecer)
        else:
            conn.execute("UPDATE nao_achado SET estoque_status='nao_solicitado' WHERE id=?", (nid,))
    return jsonify({"success": True, **final})


@naoachado_routes.post("/nao-achado/<int:nid>/reprocessar")
@login_required
def reprocessar(nid):
    """Refaz a última etapa (zerar no Bling / avisar o Velaplace) quando falhou. Só admin."""
    if not eh_admin():
        return jsonify({"error": "Somente admin."}), 403
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM nao_achado WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Ocorrência não encontrada."}), 404
        if r["status"] not in ("aceito", "cobrar"):
            return jsonify({"error": "Só depois da decisão do admin."}), 409
        if r["estoque_status"] in ("ok", "ja_zerado") and r["velaplace_status"] in ("ok", "simulado"):
            return jsonify({"error": "Nada a reprocessar."}), 409
        final = _executar_final(conn, nid, r, r["parecer"] or "")
    return jsonify({"success": True, **final})
NAEOF
python3 -m py_compile naoachado_routes.py && echo ">> naoachado_routes.py ok"

python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "naoachado_routes" in s:
    print(">> app.py ja tinha o naoachado_routes (ok)")
else:
    linha = '    from naoachado_routes import naoachado_routes as naoachado_bp\n    app.register_blueprint(naoachado_bp, url_prefix="/api")\n'
    ancoras = ['    app.register_blueprint(fefo_bp, url_prefix="/api")\n',
               '    app.register_blueprint(mapa_bp, url_prefix="/api")\n',
               "    app.register_blueprint(api_routes)\n"]
    for a in ancoras:
        if a in s:
            open(f + ".bak_naoachado", "w", encoding="utf-8").write(s)
            open(f, "w", encoding="utf-8").write(s.replace(a, a + linha, 1))
            print(">> app.py patchado (backup em app.py.bak_naoachado)")
            break
    else:
        print("!! Nao achei onde registrar. Adicione dentro de create_app():"); print(linha); raise SystemExit(1)
PYEOF
echo ""
echo "OK. Reinicie: sudo systemctl restart wms-russia.service ; depois teste: curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/nao-achado   (deve dar 401)"
echo "Prazo padrao 48h; para mudar: NAO_ACHADO_HORAS no drop-in do servico."
echo "Zerar no Bling e avisar o Velaplace ficam DESLIGADOS ate voce ligar (ver instrucoes): NAO_ACHADO_ZERAR_BLING=1, BLING_DEPOSITO_ID=10488672491, VELAPLACE_API_URL, VELAPLACE_TOKEN."
