"""Cadastro só por convite (Biel 17/11): "para ninguém achar o link e simplesmente cadastrar e VER TUDO".

O admin gera um link de cadastro (Usuários > "Gerar link de cadastro"): vale para UMA conta e expira (padrão 7 dias).
O cadastro público (POST /api/auth/register-public, que fica no código do login do servidor) passa a exigir o convite:
este módulo trava a rota antes dela rodar (before_app_request) e marca o convite como usado quando a conta é criada.
O token só aparece uma vez, na hora de gerar; no banco fica só o hash.

Rotas (url_prefix /api, no blueprint do mapa; registradas pelo tarefas_routes):
  GET  /convites                 admin: convites e situação (valendo, usado, vencido, cancelado)
  POST /convites                 admin: {nome?, dias?} -> {link, token, expira_em}
  POST /convites/<id>/cancelar   admin
  GET  /convites/validar?t=      público: o convite vale? (para a tela de cadastro)
Desliga com CADASTRO_SO_CONVITE=0 (volta o cadastro aberto de antes)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

from flask import g, jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

PG = bool(os.environ.get("DATABASE_URL"))
ROTAS_CADASTRO = ("/api/auth/register-public", "/api/auth/register", "/api/register", "/auth/register-public", "/auth/register")
_ok = False


def ligado():
    return (os.environ.get("CADASTRO_SO_CONVITE") or "1").strip() not in ("0", "false", "nao", "não", "off")


def _hash(t):
    return hashlib.sha256(str(t or "").strip().encode()).hexdigest()


def _tab():
    global _ok
    if _ok:
        return
    pk = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with db_session() as c:
        c.execute(f"""CREATE TABLE IF NOT EXISTS convites (id {pk}, token_hash TEXT NOT NULL UNIQUE, nome TEXT, criado_por TEXT,
            criado_em BIGINT, expira_em BIGINT, usado_por TEXT, usado_em BIGINT, cancelado_em BIGINT, cancelado_por TEXT)""")
    _ok = True


def _quem():
    try:
        return str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")[:80]
    except Exception:
        return ""


def _admin():
    try:
        return str(getattr(current_user, "role", "") or "").lower() == "admin"
    except Exception:
        return False


def _situacao(r, agora=None):
    agora = agora or int(time.time())
    if r["usado_em"]:
        return "usado"
    if r["cancelado_em"]:
        return "cancelado"
    if r["expira_em"] and r["expira_em"] < agora:
        return "vencido"
    return "valendo"


def _achar(token):
    if not token:
        return None
    _tab()
    with db_session() as c:
        r = c.execute("SELECT * FROM convites WHERE token_hash=?", (_hash(token),)).fetchone()
    return dict(r) if r else None


def _ehcadastro():
    p = (request.path or "").rstrip("/")
    return request.method == "POST" and any(p.endswith(x) for x in ROTAS_CADASTRO)


def registrar(bp):
    @bp.before_app_request
    def _so_com_convite():
        if not ligado() or not _ehcadastro():
            return None
        corpo = request.get_json(silent=True) or {}
        token = request.headers.get("X-Convite") or corpo.get("convite") or request.args.get("convite")
        r = _achar(token)
        if not r or _situacao(r) != "valendo":
            msg = ("Cadastro só com convite. Peça o link de cadastro ao administrador." if not r
                   else {"usado": "Esse link de cadastro já foi usado. Peça outro ao administrador.",
                         "vencido": "Esse link de cadastro venceu. Peça outro ao administrador.",
                         "cancelado": "Esse link de cadastro foi cancelado. Peça outro ao administrador."}[_situacao(r)])
            return jsonify({"success": False, "error": msg}), 403
        g._convite_id = r["id"]
        g._convite_usuario = str(corpo.get("username") or "")[:80]
        return None

    @bp.after_app_request
    def _marca_usado(resp):
        cid = getattr(g, "_convite_id", None)
        if cid and resp.status_code < 300:
            try:
                d = resp.get_json(silent=True) or {}
                if d.get("success", True):
                    with db_session() as c:
                        c.execute("UPDATE convites SET usado_por=?, usado_em=? WHERE id=? AND usado_em IS NULL",
                                  (getattr(g, "_convite_usuario", "") or "?", int(time.time()), cid))
            except Exception:
                pass
        return resp

    @bp.get("/convites")
    @login_required
    def convites_listar():
        if not _admin():
            return jsonify({"error": "Só o admin."}), 403
        _tab()
        agora = int(time.time())
        with db_session() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM convites ORDER BY criado_em DESC LIMIT 100").fetchall()]
        campos = ("id", "nome", "criado_por", "criado_em", "expira_em", "usado_por", "usado_em")
        return jsonify({"ligado": ligado(), "convites": [{**{k: r[k] for k in campos}, "situacao": _situacao(r, agora)} for r in rows]})

    @bp.post("/convites")
    @login_required
    def convites_criar():
        if not _admin():
            return jsonify({"error": "Só o admin gera link de cadastro."}), 403
        _tab()
        d = request.get_json(silent=True) or {}
        try:
            dias = max(1, min(30, int(d.get("dias") or 7)))
        except (TypeError, ValueError):
            dias = 7
        token = secrets.token_urlsafe(24)
        agora = int(time.time())
        with db_session() as c:
            c.execute("INSERT INTO convites (token_hash, nome, criado_por, criado_em, expira_em) VALUES (?,?,?,?,?)",
                      (_hash(token), str(d.get("nome") or "").strip()[:80] or None, _quem(), agora, agora + dias * 86400))
        base = (request.headers.get("Origin") or "").rstrip("/") or "https://lhvmed-exp.lhvmed.com.br"
        return jsonify({"token": token, "link": f"{base}/register?convite={token}", "expira_em": agora + dias * 86400, "dias": dias})

    @bp.post("/convites/<int:cid>/cancelar")
    @login_required
    def convites_cancelar(cid):
        if not _admin():
            return jsonify({"error": "Só o admin."}), 403
        _tab()
        with db_session() as c:
            c.execute("UPDATE convites SET cancelado_em=?, cancelado_por=? WHERE id=? AND usado_em IS NULL AND cancelado_em IS NULL",
                      (int(time.time()), _quem(), cid))
        return jsonify({"ok": True})

    @bp.get("/convites/validar")
    def convites_validar():
        if not ligado():
            return jsonify({"ok": True, "aberto": True})
        r = _achar(request.args.get("t"))
        if not r:
            return jsonify({"ok": False, "error": "Link de cadastro inválido. Peça o link ao administrador."}), 404
        st = _situacao(r)
        if st != "valendo":
            return jsonify({"ok": False, "error": {"usado": "Esse link já foi usado.", "vencido": "Esse link venceu.",
                                                     "cancelado": "Esse link foi cancelado."}[st] + " Peça outro ao administrador."}), 410
        return jsonify({"ok": True, "nome": r["nome"], "expira_em": r["expira_em"]})
