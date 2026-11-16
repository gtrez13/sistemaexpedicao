"""Módulo FULL (Biel 16/11): preparo das remessas para o Mercado Livre Full.

O ML não tem API pública que liste as remessas criadas no painel (Seller Center): a remessa NASCE NO WMS. Quem decide o
Full escolhe anúncios Full e quantidades; todo WMS aberto avisa com som (GET /full/novas); o galpão separa igual à
Tela 1 (mapa, rota, modo fácil) e cada unidade bipada devolve os dados da etiqueta do ML (código de identificação do
anúncio no Full = inventory_id, em Code 128) para a tela imprimir na Elgin.

Registrado no blueprint do mapa (url_prefix /api) por registrar(bp). Rotas:
  GET  /full/anuncios?q=&atualizar=1     anúncios Full (cache do ML; atualizar=1 relê em segundo plano)
  GET  /full/remessas[?todas=1]          remessas abertas (ou todas)
  POST /full/remessas                    {nome, obs, itens: [{chave, qtd}]}
  GET  /full/remessas/<id>
  POST /full/remessas/<id>/itens         {itens: [{chave, qtd}]}   troca os itens (só sem nada separado do item)
  POST /full/remessas/<id>/bipar         {codigo, quantidade}  -> etiqueta {codigo, titulo, variacao, qtd}
  POST /full/remessas/<id>/desfazer      {item_id?}            desfaz o último bip (do item)
  POST /full/remessas/<id>/status        {status: aberta|separada|enviada|cancelada}
  GET  /full/novas                       remessas abertas (aviso com som em qualquer tela)"""
from __future__ import annotations

import os
import re
import threading
import time

from flask import jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

_ok_tab = False
_anuncios = {"rodando": False, "em": 0, "erro": None, "n": 0}
_lock = threading.Lock()
ABERTAS = ("aberta", "separando")


def _quem():
    try:
        return str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")[:80]
    except Exception:
        return ""


def _tabelas():
    global _ok_tab
    if _ok_tab:
        return
    with db_session() as c:
        pk = "BIGSERIAL PRIMARY KEY" if os.environ.get("DATABASE_URL") else "INTEGER PRIMARY KEY AUTOINCREMENT"
        c.execute("CREATE TABLE IF NOT EXISTS full_anuncios (chave TEXT PRIMARY KEY, item_id TEXT, variation_id TEXT, inventory_id TEXT, "
                  "sku TEXT, titulo TEXT, variacao TEXT, foto TEXT, status TEXT, ean TEXT, atualizado_em BIGINT)")
        c.execute(f"CREATE TABLE IF NOT EXISTS full_remessas (id {pk}, nome TEXT, obs TEXT, status TEXT NOT NULL DEFAULT 'aberta', "
                  "criado_por TEXT, criado_em BIGINT, atualizado_em BIGINT, fechado_por TEXT, fechado_em BIGINT)")
        c.execute(f"CREATE TABLE IF NOT EXISTS full_itens (id {pk}, remessa_id BIGINT NOT NULL, chave TEXT, item_id TEXT, variation_id TEXT, "
                  "inventory_id TEXT, sku TEXT, titulo TEXT, variacao TEXT, foto TEXT, ean TEXT, qtd INTEGER NOT NULL DEFAULT 0, "
                  "separado INTEGER NOT NULL DEFAULT 0)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_full_itens_rem ON full_itens (remessa_id)")
        c.execute(f"CREATE TABLE IF NOT EXISTS full_bips (id {pk}, remessa_id BIGINT, item_id BIGINT, qtd INTEGER, por TEXT, em BIGINT, "
                  "desfeito INTEGER NOT NULL DEFAULT 0)")
    _ok_tab = True


# ───────────────────────── anúncios Full (cache do ML) ─────────────────────────
def _sku_attrs(attrs):
    for a in attrs or []:
        if str(a.get("id") or "").upper() in ("SELLER_SKU", "SELLER_CUSTOM_FIELD"):
            v = a.get("value_name") or a.get("value_id")
            if v:
                return str(v).strip()
    return None


def _ean_attrs(attrs):
    for a in attrs or []:
        if str(a.get("id") or "").upper() in ("GTIN", "EAN"):
            v = a.get("value_name")
            if v:
                return re.sub(r"\D", "", str(v).split(",")[0]) or None
    return None


def _ler_anuncios_ml(log=print):
    """-> lista de linhas para full_anuncios (uma por anúncio, ou por variação quando tem)."""
    from services.ml_service import MercadoLivreService
    ml = MercadoLivreService()
    uid = ml.user_id
    ids, off = [], 0
    while True:
        r = ml._request("GET", f"/users/{uid}/items/search", params={"logistic_type": "fulfillment", "limit": 50, "offset": off})
        if r is None or r.status_code != 200:
            raise RuntimeError(f"busca de anúncios Full: HTTP {getattr(r, 'status_code', 'sem resposta')}")
        lote = (r.json() or {}).get("results") or []
        ids += lote
        if len(lote) < 50 or off >= 2000:
            break
        off += 50
    linhas, agora = [], int(time.time())
    for i in range(0, len(ids), 20):
        r = ml._request("GET", "/items", params={"ids": ",".join(ids[i:i + 20]), "include_attributes": "all"})
        if r is None or r.status_code != 200:
            continue
        for e in r.json() or []:
            b = e.get("body") or {}
            if not b.get("id"):
                continue
            foto = (b.get("thumbnail") or "").replace("http://", "https://")
            sku = b.get("seller_custom_field") or _sku_attrs(b.get("attributes"))
            ean = _ean_attrs(b.get("attributes"))
            vars_ = b.get("variations") or []
            if vars_:
                for v in vars_:
                    nome_v = ", ".join(f"{x.get('name')}: {x.get('value_name')}" for x in v.get("attribute_combinations") or [] if x.get("value_name"))
                    linhas.append({"chave": f"{b['id']}:{v.get('id')}", "item_id": b["id"], "variation_id": str(v.get("id")),
                                   "inventory_id": v.get("inventory_id") or b.get("inventory_id"),
                                   "sku": v.get("seller_custom_field") or _sku_attrs(v.get("attributes")) or sku,
                                   "titulo": b.get("title"), "variacao": nome_v or None, "foto": foto, "status": b.get("status"),
                                   "ean": _ean_attrs(v.get("attributes")) or ean, "atualizado_em": agora})
            else:
                linhas.append({"chave": b["id"], "item_id": b["id"], "variation_id": None, "inventory_id": b.get("inventory_id"),
                               "sku": sku, "titulo": b.get("title"), "variacao": None, "foto": foto, "status": b.get("status"),
                               "ean": ean, "atualizado_em": agora})
        time.sleep(0.15)
    return linhas


def _atualizar_anuncios():
    with _lock:
        if _anuncios["rodando"]:
            return
        _anuncios["rodando"] = True
    try:
        linhas = _ler_anuncios_ml()
        _tabelas()
        with db_session() as c:
            c.execute("DELETE FROM full_anuncios")
            for x in linhas:
                c.execute("INSERT INTO full_anuncios (chave, item_id, variation_id, inventory_id, sku, titulo, variacao, foto, status, ean, atualizado_em) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(x[k] for k in ("chave", "item_id", "variation_id", "inventory_id", "sku",
                                                                                   "titulo", "variacao", "foto", "status", "ean", "atualizado_em")))
        with _lock:
            _anuncios.update(erro=None, n=len(linhas), em=int(time.time()))
    except Exception as e:
        print(f"⚠️ full: anúncios do ML falharam: {e}", flush=True)
        with _lock:
            _anuncios["erro"] = str(e)[:200]
    finally:
        with _lock:
            _anuncios["rodando"] = False


# ───────────────────────── remessas ─────────────────────────
def _rem(c, rid, com_itens=True):
    r = c.execute("SELECT * FROM full_remessas WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    its = [dict(x) for x in c.execute("SELECT * FROM full_itens WHERE remessa_id=? ORDER BY id", (rid,)).fetchall()]
    d["n_itens"] = len(its)
    d["unidades"] = sum(int(x["qtd"] or 0) for x in its)
    d["separadas"] = sum(min(int(x["separado"] or 0), int(x["qtd"] or 0)) for x in its)
    if com_itens:
        d["itens"] = its
    return d


def _itens_de(c, pedidos):
    """[{chave, qtd}] -> linhas com os dados do anúncio (cache)."""
    out = []
    for p in pedidos or []:
        try:
            q = int(p.get("qtd") or 0)
        except (TypeError, ValueError):
            q = 0
        if q <= 0:
            continue
        a = c.execute("SELECT * FROM full_anuncios WHERE chave=?", (str(p.get("chave") or ""),)).fetchone()
        if not a:
            raise LookupError(f"Anúncio {p.get('chave')} não está na lista do Full (atualize a lista).")
        out.append((dict(a), q))
    return out


def registrar(bp):
    @bp.get("/full/anuncios")
    @login_required
    def full_anuncios():
        _tabelas()
        q = (request.args.get("q") or "").strip().lower()
        with db_session() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM full_anuncios").fetchone()["n"]
            ult = c.execute("SELECT MAX(atualizado_em) AS m FROM full_anuncios").fetchone()["m"]
        if request.args.get("atualizar") or not n or (ult and time.time() - int(ult) > 6 * 3600):
            threading.Thread(target=_atualizar_anuncios, daemon=True).start()
        with db_session() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM full_anuncios ORDER BY titulo").fetchall()]
        if q:
            rows = [r for r in rows if q in " ".join(str(r.get(k) or "") for k in ("titulo", "sku", "item_id", "inventory_id", "variacao", "ean")).lower()]
        with _lock:
            st = dict(_anuncios)
        return jsonify({"anuncios": rows[:300], "total": len(rows), "atualizando": st["rodando"], "erro": st["erro"],
                        "atualizado_em": int(ult) if ult else None})

    @bp.get("/full/remessas")
    @login_required
    def full_remessas():
        _tabelas()
        todas = request.args.get("todas")
        with db_session() as c:
            sql = "SELECT id FROM full_remessas" + ("" if todas else " WHERE status IN ('aberta','separando','separada')") + " ORDER BY id DESC LIMIT 100"
            ids = [r["id"] for r in c.execute(sql).fetchall()]
            return jsonify({"remessas": [_rem(c, i, False) for i in ids]})

    @bp.post("/full/remessas")
    @login_required
    def full_criar():
        _tabelas()
        d = request.get_json(silent=True) or {}
        agora = int(time.time())
        with db_session() as c:
            try:
                itens = _itens_de(c, d.get("itens"))
            except LookupError as e:
                return jsonify({"error": str(e)}), 400
            if not itens:
                return jsonify({"error": "Escolha pelo menos um produto com quantidade."}), 400
            nome = (str(d.get("nome") or "").strip() or f"Full {time.strftime('%d/%m %H:%M', time.localtime(agora))}")[:120]
            c.execute("INSERT INTO full_remessas (nome, obs, status, criado_por, criado_em, atualizado_em) VALUES (?,?,?,?,?,?)",
                      (nome, (str(d.get("obs") or "")[:500] or None), "aberta", _quem(), agora, agora))
            rid = c.execute("SELECT MAX(id) AS m FROM full_remessas WHERE criado_em=? AND nome=?", (agora, nome)).fetchone()["m"]
            for a, q in itens:
                c.execute("INSERT INTO full_itens (remessa_id, chave, item_id, variation_id, inventory_id, sku, titulo, variacao, foto, ean, qtd) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, a["chave"], a["item_id"], a["variation_id"], a["inventory_id"], a["sku"],
                                                             a["titulo"], a["variacao"], a["foto"], a["ean"], q))
            return jsonify({"success": True, "remessa": _rem(c, rid)})

    @bp.get("/full/remessas/<int:rid>")
    @login_required
    def full_ver(rid):
        _tabelas()
        with db_session() as c:
            d = _rem(c, rid)
        return jsonify(d) if d else (jsonify({"error": "Remessa não encontrada."}), 404)

    @bp.post("/full/remessas/<int:rid>/itens")
    @login_required
    def full_itens(rid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        with db_session() as c:
            r = _rem(c, rid)
            if not r:
                return jsonify({"error": "Remessa não encontrada."}), 404
            if r["status"] not in ABERTAS:
                return jsonify({"error": "Remessa já fechada."}), 409
            try:
                novos = {a["chave"]: (a, q) for a, q in _itens_de(c, d.get("itens"))}
            except LookupError as e:
                return jsonify({"error": str(e)}), 400
            for it in r["itens"]:
                if it["chave"] in novos:
                    _, q = novos.pop(it["chave"])
                    if q < int(it["separado"] or 0):
                        return jsonify({"error": f"{it['titulo'][:40]}: já separaram {it['separado']}, não dá para pedir menos."}), 409
                    c.execute("UPDATE full_itens SET qtd=? WHERE id=?", (q, it["id"]))
                elif int(it["separado"] or 0) == 0:
                    c.execute("DELETE FROM full_itens WHERE id=?", (it["id"],))
                else:
                    return jsonify({"error": f"{it['titulo'][:40]} já começou a ser separado: não dá para tirar."}), 409
            for a, q in novos.values():
                c.execute("INSERT INTO full_itens (remessa_id, chave, item_id, variation_id, inventory_id, sku, titulo, variacao, foto, ean, qtd) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, a["chave"], a["item_id"], a["variation_id"], a["inventory_id"], a["sku"],
                                                             a["titulo"], a["variacao"], a["foto"], a["ean"], q))
            c.execute("UPDATE full_remessas SET atualizado_em=? WHERE id=?", (int(time.time()), rid))
            return jsonify({"success": True, "remessa": _rem(c, rid)})

    @bp.post("/full/remessas/<int:rid>/bipar")
    @login_required
    def full_bipar(rid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        cod = str(d.get("codigo") or "").strip()
        if "*" in cod:                          # etiqueta do WMS: SKU*LOTE(*CÓDIGO)
            cod = cod.split("*")[0].strip()
        try:
            qtd = max(1, int(d.get("quantidade") or 1))
        except (TypeError, ValueError):
            qtd = 1
        k = cod.upper()
        kd = re.sub(r"\D", "", cod).lstrip("0")
        with db_session() as c:
            r = _rem(c, rid)
            if not r:
                return jsonify({"success": False, "error": "Remessa não encontrada."}), 404
            if r["status"] not in ABERTAS:
                return jsonify({"success": False, "error": "Remessa já fechada."}), 409
            achados = [it for it in r["itens"] if k and (str(it["sku"] or "").upper() == k or str(it["inventory_id"] or "").upper() == k
                       or (kd and str(it["ean"] or "").lstrip("0") == kd))]
            if not achados:
                return jsonify({"success": False, "error": f"{cod} não está nesta remessa.", "tipo": "fora"}), 404
            it = next((x for x in achados if int(x["separado"]) < int(x["qtd"])), None)
            if not it:
                tot = sum(int(x["qtd"]) for x in achados)
                return jsonify({"success": False, "error": f"{achados[0]['titulo'][:50]} já está completo ({tot} de {tot}).", "tipo": "completo"}), 409
            dar = min(qtd, int(it["qtd"]) - int(it["separado"]))
            c.execute("UPDATE full_itens SET separado=separado+? WHERE id=?", (dar, it["id"]))
            c.execute("INSERT INTO full_bips (remessa_id, item_id, qtd, por, em) VALUES (?,?,?,?,?)", (rid, it["id"], dar, _quem(), int(time.time())))
            c.execute("UPDATE full_remessas SET status='separando', atualizado_em=? WHERE id=? AND status='aberta'", (int(time.time()), rid))
            novo = _rem(c, rid)
        itn = next(x for x in novo["itens"] if x["id"] == it["id"])
        tot = sum(int(x["qtd"]) for x in novo["itens"] if x["chave"] == it["chave"])
        sep = sum(int(x["separado"]) for x in novo["itens"] if x["chave"] == it["chave"])
        return jsonify({"success": True, "item": itn, "bipado_agora": dar, "sku": it["sku"], "nome": it["titulo"],
                        "produto": {"total": tot, "bipado": sep, "falta": tot - sep}, "remessa": {k2: novo[k2] for k2 in ("unidades", "separadas", "status")},
                        "etiqueta": {"codigo": it["inventory_id"], "titulo": it["titulo"], "variacao": it["variacao"], "qtd": dar,
                                     "sem_codigo": not it["inventory_id"]}})

    @bp.post("/full/remessas/<int:rid>/desfazer")
    @login_required
    def full_desfazer(rid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        with db_session() as c:
            sql, args = "SELECT * FROM full_bips WHERE remessa_id=? AND desfeito=0", [rid]
            if d.get("item_id"):
                sql += " AND item_id=?"; args.append(int(d["item_id"]))
            b = c.execute(sql + " ORDER BY id DESC LIMIT 1", tuple(args)).fetchone()
            if not b:
                return jsonify({"success": False, "error": "Nada para desfazer."}), 404
            c.execute("UPDATE full_bips SET desfeito=1 WHERE id=?", (b["id"],))
            c.execute("UPDATE full_itens SET separado=CASE WHEN separado>=? THEN separado-? ELSE 0 END WHERE id=?", (b["qtd"], b["qtd"], b["item_id"]))
            return jsonify({"success": True, "desfeito": int(b["qtd"]), "remessa": _rem(c, rid)})

    @bp.post("/full/remessas/<int:rid>/status")
    @login_required
    def full_status(rid):
        _tabelas()
        st = str((request.get_json(silent=True) or {}).get("status") or "")
        if st not in ("aberta", "separada", "enviada", "cancelada"):
            return jsonify({"error": "status inválido"}), 400
        with db_session() as c:
            r = _rem(c, rid, False)
            if not r:
                return jsonify({"error": "Remessa não encontrada."}), 404
            if st == "cancelada" and r["separadas"] > 0:
                return jsonify({"error": "Já tem unidade separada: desfaça os bips antes de cancelar."}), 409
            c.execute("UPDATE full_remessas SET status=?, atualizado_em=?, fechado_por=?, fechado_em=? WHERE id=?",
                      (st, int(time.time()), _quem() if st != "aberta" else None, int(time.time()) if st != "aberta" else None, rid))
            return jsonify({"success": True, "remessa": _rem(c, rid, False)})

    @bp.get("/full/novas")
    @login_required
    def full_novas():
        _tabelas()
        with db_session() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM full_remessas WHERE status IN ('aberta','separando') AND criado_em > ? "
                                              "ORDER BY id DESC LIMIT 10", (int(time.time()) - 3 * 86400,)).fetchall()]
            return jsonify({"remessas": [_rem(c, i, False) for i in ids], "agora": int(time.time())})
