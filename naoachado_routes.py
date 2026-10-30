"""
"Não achado em estoque" (backend).

Fluxo: o operador procura o produto (mapa do galpão mostra onde fica), não acha e registra.
O SUPERVISOR de logística tem NAO_ACHADO_HORAS (padrão 48) para EXPLICAR o motivo; depois o ADMIN
dá o parecer (aceitar ou cobrar). Ocorrência sem explicação no prazo fica "vencida" (cobrável).

Endpoints (url_prefix="/api"; todos exigem login):
    POST /api/nao-achado                 operador/qualquer logado: {sku, nome?, qtd?, ml_id?, estoque_bling?, locais?, obs?}
    GET  /api/nao-achado[?status=]       supervisor/admin: lista (aberto|vencido|explicado|aceito|cobrar|todos)
    POST /api/nao-achado/<id>/explicar   supervisor/admin: {texto} (mín. 10 caracteres)
    POST /api/nao-achado/<id>/decidir    admin: {decisao: aceito|cobrar, parecer?}
    GET  /api/nao-achado/resumo          supervisor/admin: contadores (abertos, vencidos, a decidir)

Só banco local: não chama o Bling.
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

    if conn is not None:
        _run(conn)
    else:
        with db_session() as c:
            _run(c)
    _tabelas_ok = True


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
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT status, prazo_em FROM nao_achado WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Ocorrência não encontrada."}), 404
        if r["status"] == "aceito" or r["status"] == "cobrar":
            return jsonify({"error": "Já decidida."}), 409
        conn.execute("UPDATE nao_achado SET status=?, parecer=?, decidido_por=?, decidido_em=? WHERE id=?",
                     (dec, (d.get("parecer") or "")[:500], _quem(), int(time.time()), nid))
    return jsonify({"success": True})
