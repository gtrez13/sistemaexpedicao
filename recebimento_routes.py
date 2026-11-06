"""
Módulo RECEBIMENTO (entrada de NF) — LHVMed.

Fluxo: XML chega (e-mail / upload / chave) -> nota fica AGUARDANDO -> operador abre, vincula o pedido de compra
(automático pelo fornecedor), bipa os produtos (quantidade, lote, validade) -> FECHAR:
   * tudo bateu            -> CONFERIDA  (aí sim pode lançar no Bling)
   * qualquer divergência  -> RECUSADA   (motivos em vermelho; só supervisor/admin resolve)

Endpoints (url_prefix="/api", login):
    POST /recebimento/xml                    multipart "xml" ou JSON {xml}: importa/atualiza a nota
    POST /recebimento/chave                  {chave}: acha a nota (local -> Meu Danfe se configurado)
    GET  /recebimento/notas?status=          lista
    GET  /recebimento/notas/<id>             nota + itens + pedidos de compra candidatos
    POST /recebimento/notas/<id>/pedido      {pedido_id|null}: vincula/desvincula pedido de compra (Bling)
    POST /recebimento/notas/<id>/bipar       {codigo, quantidade=1, lote?, validade?}
    POST /recebimento/notas/<id>/itens/<iid> {sku?, qtd_conferida?, lote?, validade?}  (aprende cProd->SKU)
    POST /recebimento/notas/<id>/fechar      conferida / recusada
    POST /recebimento/notas/<id>/resolver    gestor {texto}: libera uma recusada
    POST /recebimento/notas/<id>/reabrir     volta para conferindo
    GET  /recebimento/produtos?q=            busca de SKU pra vincular
    GET  /recebimento/resumo

Regras: RECEB_EXIGIR_LOTE=1 (padrão) exige lote E validade em todo item; 0 só avisa.
Só LÊ o Bling (pedidos de compra / contatos). Nunca lança nota.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

recebimento_routes = Blueprint("recebimento_routes", __name__)
NS = {"n": "http://www.portalfiscal.inf.br/nfe"}
STATUS = ["aguardando", "conferindo", "recusada", "conferida", "lancada"]
EXIGIR_LOTE = os.environ.get("RECEB_EXIGIR_LOTE", "1").strip() not in ("0", "false", "nao", "não")

_tabelas_ok = False
_pc_cache: dict = {}      # cnpj -> (ts, lista)


# ───────────────────────── util ─────────────────────────
def _quem():
    try:
        return getattr(current_user, "username", None) or getattr(current_user, "email", None) or str(current_user.id)
    except Exception:
        return None


def eh_gestor() -> bool:
    try:
        return (getattr(current_user, "role", "") or "").lower() in ("admin", "supervisor")
    except Exception:
        return False


def _agora() -> int:
    return int(time.time())


def _dig(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def garantir_tabelas():
    global _tabelas_ok
    if _tabelas_ok:
        return
    with db_session() as c:
        pg = True
        try:
            c.execute("SELECT 1 FROM information_schema.tables LIMIT 1").fetchone()
        except Exception:
            pg = False
        pk = "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS receb_notas (
                id {pk}, chave TEXT UNIQUE NOT NULL, numero TEXT, serie TEXT, emissao TEXT,
                fornecedor_cnpj TEXT, fornecedor_nome TEXT, valor NUMERIC, xml TEXT,
                status TEXT NOT NULL DEFAULT 'aguardando',
                pedido_compra_id BIGINT, pedido_compra_numero TEXT,
                criado_em BIGINT, origem TEXT, conferido_por TEXT, conferido_em BIGINT,
                motivos TEXT, resolucao TEXT, resolvido_por TEXT, resolvido_em BIGINT, atualizado_em BIGINT
            )""")
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS receb_itens (
                id {pk}, nota_id BIGINT NOT NULL, n_item INTEGER, cprod TEXT, ean TEXT, descricao TEXT, ncm TEXT,
                unidade TEXT, qtd_nf NUMERIC, valor_unit NUMERIC, lote_xml TEXT, validade_xml TEXT,
                sku TEXT, qtd_conferida NUMERIC DEFAULT 0, lote TEXT, validade TEXT, obs TEXT,
                pc_qtd NUMERIC, pc_item TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_receb_itens_nota ON receb_itens (nota_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS receb_vinculos (
                fornecedor_cnpj TEXT NOT NULL, cprod TEXT NOT NULL, sku TEXT NOT NULL,
                criado_por TEXT, criado_em BIGINT, PRIMARY KEY (fornecedor_cnpj, cprod)
            )""")
    _tabelas_ok = True


# ───────────────────────── XML ─────────────────────────
def parse_nfe(xml_txt: str) -> dict:
    root = ET.fromstring(xml_txt.encode("utf-8") if isinstance(xml_txt, str) else xml_txt)
    inf = root.find(".//n:infNFe", NS)
    if inf is None:
        raise ValueError("XML não é uma NF-e (sem infNFe)")
    g = lambda el, p: (el.findtext(p, default="", namespaces=NS) or "").strip() if el is not None else ""
    ide, emit, tot = inf.find("n:ide", NS), inf.find("n:emit", NS), inf.find("n:total/n:ICMSTot", NS)
    chave = (inf.get("Id") or "")[3:]
    itens = []
    for det in inf.findall("n:det", NS):
        p = det.find("n:prod", NS)
        ras = p.find("n:rastro", NS) if p is not None else None
        itens.append({
            "n_item": int(det.get("nItem") or len(itens) + 1),
            "cprod": g(p, "n:cProd"), "ean": _dig(g(p, "n:cEAN")) if g(p, "n:cEAN").upper() != "SEM GTIN" else "",
            "descricao": g(p, "n:xProd"), "ncm": g(p, "n:NCM"), "unidade": g(p, "n:uCom"),
            "qtd_nf": float(g(p, "n:qCom") or 0), "valor_unit": float(g(p, "n:vUnCom") or 0),
            "lote_xml": g(ras, "n:nLote"), "validade_xml": g(ras, "n:dVal"),
        })
    return {
        "chave": chave, "numero": g(ide, "n:nNF"), "serie": g(ide, "n:serie"), "emissao": g(ide, "n:dhEmi")[:10],
        "fornecedor_cnpj": _dig(g(emit, "n:CNPJ") or g(emit, "n:CPF")), "fornecedor_nome": g(emit, "n:xNome"),
        "valor": float(g(tot, "n:vNF") or 0), "itens": itens,
    }


def _importar_xml(xml_txt: str, origem: str) -> dict:
    nf = parse_nfe(xml_txt)
    if not nf["chave"] or len(nf["chave"]) != 44:
        raise ValueError("chave de acesso inválida no XML")
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT id, status FROM receb_notas WHERE chave=?", (nf["chave"],)).fetchone()
        if r:
            if not c.execute("SELECT xml FROM receb_notas WHERE id=?", (r["id"],)).fetchone()["xml"]:
                c.execute("UPDATE receb_notas SET xml=?, atualizado_em=? WHERE id=?", (xml_txt, _agora(), r["id"]))
            return {"id": r["id"], "ja_existia": True, "status": r["status"]}
        c.execute("""INSERT INTO receb_notas (chave, numero, serie, emissao, fornecedor_cnpj, fornecedor_nome, valor, xml,
                        status, criado_em, origem, atualizado_em) VALUES (?,?,?,?,?,?,?,?,'aguardando',?,?,?)""",
                  (nf["chave"], nf["numero"], nf["serie"], nf["emissao"], nf["fornecedor_cnpj"], nf["fornecedor_nome"],
                   nf["valor"], xml_txt, _agora(), origem, _agora()))
        nid = c.execute("SELECT id FROM receb_notas WHERE chave=?", (nf["chave"],)).fetchone()["id"]
        vinc = {v["cprod"]: v["sku"] for v in c.execute("SELECT cprod, sku FROM receb_vinculos WHERE fornecedor_cnpj=?", (nf["fornecedor_cnpj"],)).fetchall()}
        for it in nf["itens"]:
            sku = vinc.get(it["cprod"]) or _sku_existente(c, it["cprod"], it["ean"])
            c.execute("""INSERT INTO receb_itens (nota_id, n_item, cprod, ean, descricao, ncm, unidade, qtd_nf, valor_unit,
                            lote_xml, validade_xml, sku, lote, validade) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (nid, it["n_item"], it["cprod"], it["ean"], it["descricao"], it["ncm"], it["unidade"], it["qtd_nf"],
                       it["valor_unit"], it["lote_xml"], it["validade_xml"], sku, it["lote_xml"] or None, it["validade_xml"] or None))
    return {"id": nid, "ja_existia": False, "status": "aguardando"}


def _sku_existente(c, cprod: str, ean: str):
    """cProd do fornecedor igual a um SKU nosso? ou EAN conhecido? (tabela produtos do WMS)"""
    for cand in [x for x in (cprod, ean) if x]:
        try:
            r = c.execute("SELECT sku FROM produtos WHERE UPPER(sku)=UPPER(?) LIMIT 1", (cand,)).fetchone()
            if r:
                return r["sku"]
        except Exception:
            pass
    return None


# ───────────────────────── Bling (só leitura) ─────────────────────────
def _bling():
    try:
        from services.bling_service import BlingService
        return BlingService()
    except Exception:
        return None


def pedidos_compra_do_fornecedor(cnpj: str) -> list:
    """Pedidos de compra abertos/em andamento do fornecedor (Bling), com itens. Cache 10 min."""
    cnpj = _dig(cnpj)
    agora = time.time()
    if cnpj in _pc_cache and agora - _pc_cache[cnpj][0] < 600:
        return _pc_cache[cnpj][1]
    out = []
    b = _bling()
    if b and cnpj:
        try:
            ct = b._get_json("/contatos", {"numeroDocumento": cnpj, "limite": 5}).get("data") or []
            ids = [x["id"] for x in ct if _dig(x.get("numeroDocumento")) == cnpj]
            for fid in ids[:2]:
                for sit in (0, 3):      # em aberto, em andamento
                    lst = b._get_json("/pedidos/compras", {"idFornecedor": fid, "idsSituacoes[]": sit, "limite": 50}).get("data") or []
                    for pc in lst:
                        det = (b._get_json(f"/pedidos/compras/{pc['id']}") or {}).get("data") or {}
                        out.append({
                            "id": pc["id"], "numero": pc.get("numero"), "data": pc.get("data"), "prevista": pc.get("dataPrevista"),
                            "total": pc.get("total"), "situacao": (pc.get("situacao") or {}).get("valor"),
                            "itens": [{"sku": (i.get("produto") or {}).get("codigo"), "cprod": i.get("codigoFornecedor"),
                                       "descricao": i.get("descricao"), "qtd": i.get("quantidade"),
                                       "atendido": (i.get("notaFiscal") or {}).get("quantidade") or 0, "valor": i.get("valor")}
                                      for i in det.get("itens") or []],
                        })
                        time.sleep(0.35)
        except Exception as e:
            print(f"⚠️ recebimento: pedidos de compra do Bling falharam: {e}", flush=True)
    _pc_cache[cnpj] = (agora, out)
    return out


# ───────────────────────── Meu Danfe (opcional) ─────────────────────────
def buscar_xml_por_chave(chave: str):
    """Plano C: Meu Danfe. Configure MEUDANFE_URL (com {chave}) e MEUDANFE_TOKEN. Sem isso devolve None."""
    url, tok = os.environ.get("MEUDANFE_URL"), os.environ.get("MEUDANFE_TOKEN")
    if not url or not tok:
        return None
    try:
        import requests
        r = requests.get(url.format(chave=chave), headers={"Authorization": f"Bearer {tok}", "Accept": "application/xml"}, timeout=20)
        if r.status_code == 200 and b"infNFe" in r.content:
            return r.text
    except Exception as e:
        print(f"⚠️ meudanfe: {e}", flush=True)
    return None


# ───────────────────────── leitura ─────────────────────────
def _nota_dict(c, nid: int, com_pc: bool = True) -> dict | None:
    r = c.execute("SELECT * FROM receb_notas WHERE id=?", (nid,)).fetchone()
    if not r:
        return None
    d = {k: r[k] for k in r.keys() if k != "xml"}
    d["tem_xml"] = bool(r["xml"])
    d["valor"] = float(r["valor"] or 0)
    itens = [dict(x) for x in c.execute("SELECT * FROM receb_itens WHERE nota_id=? ORDER BY n_item", (nid,)).fetchall()]
    for it in itens:
        it["qtd_nf"] = float(it["qtd_nf"] or 0); it["qtd_conferida"] = float(it["qtd_conferida"] or 0)
        it["pc_qtd"] = float(it["pc_qtd"]) if it["pc_qtd"] is not None else None
    d["itens"] = itens
    d["motivos"] = [m for m in (r["motivos"] or "").split("\n") if m]
    if com_pc:
        pcs = pedidos_compra_do_fornecedor(r["fornecedor_cnpj"])
        d["pedidos_compra"] = [{k: v for k, v in pc.items() if k != "itens"} | {"n_itens": len(pc["itens"])} for pc in pcs]
        d["pedido_compra"] = next((pc for pc in pcs if pc["id"] == r["pedido_compra_id"]), None)
    d["exigir_lote"] = EXIGIR_LOTE
    d["eh_gestor"] = eh_gestor()
    return d


def _divergencias(d: dict) -> list:
    m = []
    pc = d.get("pedido_compra")
    for it in d["itens"]:
        nome = it["descricao"][:40]
        if not it["sku"]:
            m.append(f"Item {it['n_item']} ({nome}): sem SKU vinculado")
        if it["qtd_conferida"] < it["qtd_nf"]:
            m.append(f"Item {it['n_item']} ({nome}): FALTOU — NF {it['qtd_nf']:g}, conferido {it['qtd_conferida']:g}")
        elif it["qtd_conferida"] > it["qtd_nf"]:
            m.append(f"Item {it['n_item']} ({nome}): SOBROU — NF {it['qtd_nf']:g}, conferido {it['qtd_conferida']:g}")
        if EXIGIR_LOTE and (not (it["lote"] or "").strip() or not (it["validade"] or "").strip()):
            m.append(f"Item {it['n_item']} ({nome}): sem lote/validade")
        if pc:
            pi = next((x for x in pc["itens"] if (x["sku"] and it["sku"] and str(x["sku"]).upper() == str(it["sku"]).upper())
                       or (x["cprod"] and it["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper())), None)
            if not pi:
                m.append(f"Item {it['n_item']} ({nome}): NÃO está no pedido de compra {pc['numero']}")
            else:
                falta_pc = float(pi["qtd"] or 0) - float(pi["atendido"] or 0)
                if it["qtd_nf"] > falta_pc + 1e-9:
                    m.append(f"Item {it['n_item']} ({nome}): NF traz {it['qtd_nf']:g}, pedido só tem {falta_pc:g} em aberto")
    if not pc:
        m.append("Nenhum pedido de compra vinculado")
    return m


# ───────────────────────── rotas ─────────────────────────
@recebimento_routes.post("/recebimento/xml")
@login_required
def receber_xml():
    f = request.files.get("xml")
    txt = f.read().decode("utf-8", "ignore") if f else (request.get_json(silent=True) or {}).get("xml")
    if not txt:
        return jsonify({"success": False, "error": "Envie o XML (campo 'xml')."}), 400
    try:
        r = _importar_xml(txt, "upload")
    except Exception as e:
        return jsonify({"success": False, "error": f"XML inválido: {e}"}), 400
    return jsonify({"success": True, **r})


@recebimento_routes.post("/recebimento/chave")
@login_required
def por_chave():
    chave = _dig((request.get_json(silent=True) or {}).get("chave"))
    if len(chave) != 44:
        return jsonify({"success": False, "error": f"Chave precisa ter 44 dígitos (veio {len(chave)})."}), 400
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT id, status FROM receb_notas WHERE chave=?", (chave,)).fetchone()
    if r:
        return jsonify({"success": True, "id": r["id"], "status": r["status"]})
    xml = buscar_xml_por_chave(chave)
    if xml:
        try:
            return jsonify({"success": True, **_importar_xml(xml, "meudanfe")})
        except Exception as e:
            return jsonify({"success": False, "error": f"XML do Meu Danfe inválido: {e}"}), 502
    return jsonify({"success": False, "nao_encontrada": True, "chave": chave,
                    "error": "XML dessa nota ainda não chegou. Anexe o XML (e-mail do fornecedor) ou aguarde a sincronização do e-mail."}), 404


@recebimento_routes.get("/recebimento/notas")
@login_required
def listar():
    garantir_tabelas()
    st = request.args.get("status")
    sql, args = "SELECT id, chave, numero, serie, emissao, fornecedor_nome, fornecedor_cnpj, valor, status, pedido_compra_numero, criado_em, conferido_por, motivos FROM receb_notas", []
    if st and st in STATUS:
        sql += " WHERE status=?"; args.append(st)
    sql += " ORDER BY id DESC LIMIT 300"
    with db_session() as c:
        rows = c.execute(sql, tuple(args)).fetchall()
        out = []
        for r in rows:
            d = dict(r); d["valor"] = float(d["valor"] or 0)
            d["n_motivos"] = len([m for m in (r["motivos"] or "").split("\n") if m]); d.pop("motivos", None)
            cnt = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN qtd_conferida>=qtd_nf THEN 1 ELSE 0 END),0) AS ok FROM receb_itens WHERE nota_id=?", (r["id"],)).fetchone()
            d["n_itens"], d["itens_ok"] = int(cnt["n"]), int(cnt["ok"])
            out.append(d)
    return jsonify({"notas": out})


@recebimento_routes.get("/recebimento/resumo")
@login_required
def resumo():
    garantir_tabelas()
    with db_session() as c:
        por = {r["status"]: int(r["n"]) for r in c.execute("SELECT status, COUNT(*) AS n FROM receb_notas GROUP BY status").fetchall()}
    return jsonify({"por_status": por, "exigir_lote": EXIGIR_LOTE})


@recebimento_routes.get("/recebimento/notas/<int:nid>")
@login_required
def detalhe(nid):
    garantir_tabelas()
    with db_session() as c:
        d = _nota_dict(c, nid)
        if not d:
            return jsonify({"error": "Nota não encontrada."}), 404
        if d["status"] == "aguardando":
            c.execute("UPDATE receb_notas SET status='conferindo', atualizado_em=? WHERE id=?", (_agora(), nid)); d["status"] = "conferindo"
        # vincula pedido de compra sozinho quando só tem um candidato
        if not d["pedido_compra_id"] and len(d.get("pedidos_compra") or []) == 1:
            pc = d["pedidos_compra"][0]
            c.execute("UPDATE receb_notas SET pedido_compra_id=?, pedido_compra_numero=? WHERE id=?", (pc["id"], str(pc["numero"]), nid))
            d = _nota_dict(c, nid)
    return jsonify(d)


@recebimento_routes.post("/recebimento/notas/<int:nid>/pedido")
@login_required
def vincular_pedido(nid):
    pid = (request.get_json(silent=True) or {}).get("pedido_id")
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT fornecedor_cnpj FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Nota não encontrada."}), 404
        if pid:
            pc = next((p for p in pedidos_compra_do_fornecedor(r["fornecedor_cnpj"]) if str(p["id"]) == str(pid)), None)
            if not pc:
                return jsonify({"error": "Pedido de compra não é desse fornecedor."}), 400
            c.execute("UPDATE receb_notas SET pedido_compra_id=?, pedido_compra_numero=?, atualizado_em=? WHERE id=?", (pc["id"], str(pc["numero"]), _agora(), nid))
        else:
            c.execute("UPDATE receb_notas SET pedido_compra_id=NULL, pedido_compra_numero=NULL, atualizado_em=? WHERE id=?", (_agora(), nid))
        d = _nota_dict(c, nid)
    return jsonify(d)


def _acha_item(itens, codigo: str):
    k = str(codigo or "").strip().upper()
    for campo in ("sku", "ean", "cprod"):
        for it in itens:
            if it.get(campo) and str(it[campo]).upper() == k:
                return it
    return None


@recebimento_routes.post("/recebimento/notas/<int:nid>/bipar")
@login_required
def bipar(nid):
    d = request.get_json(silent=True) or {}
    codigo = str(d.get("codigo") or "").strip()
    try:
        qtd = float(d.get("quantidade") or 1)
    except (TypeError, ValueError):
        qtd = 1
    if not codigo or qtd <= 0:
        return jsonify({"success": False, "error": "Código/quantidade inválidos."}), 400
    garantir_tabelas()
    with db_session() as c:
        nota = _nota_dict(c, nid, com_pc=False)
        if not nota:
            return jsonify({"success": False, "error": "Nota não encontrada."}), 404
        if nota["status"] in ("conferida", "lancada"):
            return jsonify({"success": False, "error": "Nota já conferida — reabra para alterar."}), 409
        it = _acha_item(nota["itens"], codigo)
        if not it:
            return jsonify({"success": False, "error": f"{codigo} não está nesta nota. Confira o produto ou vincule o SKU no item certo.", "nao_na_nota": True}), 404
        sets, args = ["qtd_conferida = COALESCE(qtd_conferida,0) + ?"], [qtd]
        if d.get("lote"):
            sets.append("lote=?"); args.append(str(d["lote"]).strip()[:60])
        if d.get("validade"):
            sets.append("validade=?"); args.append(str(d["validade"]).strip()[:10])
        c.execute(f"UPDATE receb_itens SET {', '.join(sets)} WHERE id=?", tuple(args) + (it["id"],))
        c.execute("UPDATE receb_notas SET status='conferindo', atualizado_em=? WHERE id=? AND status IN ('aguardando','recusada','conferindo')", (_agora(), nid))
        novo = dict(c.execute("SELECT * FROM receb_itens WHERE id=?", (it["id"],)).fetchone())
    novo["qtd_nf"] = float(novo["qtd_nf"] or 0); novo["qtd_conferida"] = float(novo["qtd_conferida"] or 0)
    falta = novo["qtd_nf"] - novo["qtd_conferida"]
    return jsonify({"success": True, "item": novo, "falta": falta, "excedeu": falta < 0,
                    "precisa_lote": EXIGIR_LOTE and not (novo["lote"] and novo["validade"])})


@recebimento_routes.post("/recebimento/notas/<int:nid>/itens/<int:iid>")
@login_required
def editar_item(nid, iid):
    d = request.get_json(silent=True) or {}
    garantir_tabelas()
    with db_session() as c:
        it = c.execute("SELECT * FROM receb_itens WHERE id=? AND nota_id=?", (iid, nid)).fetchone()
        if not it:
            return jsonify({"error": "Item não encontrado."}), 404
        sets, args = [], []
        if "sku" in d:
            sku = str(d["sku"] or "").strip()
            sets.append("sku=?"); args.append(sku or None)
            if sku and it["cprod"]:
                cnpj = c.execute("SELECT fornecedor_cnpj FROM receb_notas WHERE id=?", (nid,)).fetchone()["fornecedor_cnpj"]
                c.execute("DELETE FROM receb_vinculos WHERE fornecedor_cnpj=? AND cprod=?", (cnpj, it["cprod"]))
                c.execute("INSERT INTO receb_vinculos (fornecedor_cnpj, cprod, sku, criado_por, criado_em) VALUES (?,?,?,?,?)", (cnpj, it["cprod"], sku, _quem(), _agora()))
        for campo in ("lote", "validade", "obs"):
            if campo in d:
                sets.append(f"{campo}=?"); args.append((str(d[campo]).strip() or None) if d[campo] is not None else None)
        if "qtd_conferida" in d:
            try:
                sets.append("qtd_conferida=?"); args.append(max(0.0, float(d["qtd_conferida"] or 0)))
            except (TypeError, ValueError):
                pass
        if sets:
            c.execute(f"UPDATE receb_itens SET {', '.join(sets)} WHERE id=?", tuple(args) + (iid,))
            c.execute("UPDATE receb_notas SET atualizado_em=? WHERE id=?", (_agora(), nid))
        novo = dict(c.execute("SELECT * FROM receb_itens WHERE id=?", (iid,)).fetchone())
    novo["qtd_nf"] = float(novo["qtd_nf"] or 0); novo["qtd_conferida"] = float(novo["qtd_conferida"] or 0)
    return jsonify({"success": True, "item": novo})


@recebimento_routes.post("/recebimento/notas/<int:nid>/fechar")
@login_required
def fechar(nid):
    garantir_tabelas()
    with db_session() as c:
        d = _nota_dict(c, nid)
        if not d:
            return jsonify({"error": "Nota não encontrada."}), 404
        motivos = _divergencias(d)
        agora = _agora()
        if motivos:
            c.execute("UPDATE receb_notas SET status='recusada', motivos=?, conferido_por=?, conferido_em=?, atualizado_em=? WHERE id=?",
                      ("\n".join(motivos), _quem(), agora, agora, nid))
            return jsonify({"success": True, "status": "recusada", "motivos": motivos})
        c.execute("UPDATE receb_notas SET status='conferida', motivos=NULL, conferido_por=?, conferido_em=?, atualizado_em=? WHERE id=?",
                  (_quem(), agora, agora, nid))
    return jsonify({"success": True, "status": "conferida", "motivos": []})


@recebimento_routes.post("/recebimento/notas/<int:nid>/resolver")
@login_required
def resolver(nid):
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin libera uma nota recusada."}), 403
    texto = ((request.get_json(silent=True) or {}).get("texto") or "").strip()
    if len(texto) < 10:
        return jsonify({"error": "Explique a liberação (mín. 10 caracteres)."}), 400
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT status FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Nota não encontrada."}), 404
        if r["status"] != "recusada":
            return jsonify({"error": "Só nota RECUSADA pode ser liberada."}), 409
        agora = _agora()
        c.execute("UPDATE receb_notas SET status='conferida', resolucao=?, resolvido_por=?, resolvido_em=?, atualizado_em=? WHERE id=?",
                  (texto[:1000], _quem(), agora, agora, nid))
    return jsonify({"success": True, "status": "conferida"})


@recebimento_routes.post("/recebimento/notas/<int:nid>/reabrir")
@login_required
def reabrir(nid):
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT status FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Nota não encontrada."}), 404
        if r["status"] == "conferida" and not eh_gestor():
            return jsonify({"error": "Nota conferida: só supervisor/admin reabre."}), 403
        c.execute("UPDATE receb_notas SET status='conferindo', motivos=NULL, atualizado_em=? WHERE id=?", (_agora(), nid))
    return jsonify({"success": True, "status": "conferindo"})


@recebimento_routes.get("/recebimento/produtos")
@login_required
def produtos():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"produtos": []})
    out = []
    with db_session() as c:
        try:
            rows = c.execute("SELECT sku, nome FROM produtos WHERE UPPER(sku) LIKE UPPER(?) OR UPPER(nome) LIKE UPPER(?) ORDER BY sku LIMIT 15", (f"%{q}%", f"%{q}%")).fetchall()
            out = [{"sku": r["sku"], "nome": r["nome"]} for r in rows]
        except Exception:
            pass
        if len(out) < 15:
            try:
                rows = c.execute("SELECT sku_key, nome FROM mapa_produto_cache WHERE sku_key LIKE ? OR UPPER(nome) LIKE UPPER(?) ORDER BY sku_key LIMIT 15", (f"%{q.lower()}%", f"%{q}%")).fetchall()
                ja = {x["sku"].lower() for x in out}
                out += [{"sku": r["sku_key"], "nome": r["nome"]} for r in rows if r["sku_key"] not in ja]
            except Exception:
                pass
    return jsonify({"produtos": out[:15]})
