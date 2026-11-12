#!/usr/bin/env bash
# Instala o RECEBIMENTO (entrada de NF conferida). Rode DENTRO da pasta do backend (app.py).
#   bash instalar_recebimento.sh          -> instala/atualiza o modulo
#   bash instalar_recebimento.sh --cron   -> liga a leitura do e-mail a cada 10 min (precisa RECEB_IMAP_* no .env)
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

if [ "${1:-}" != "--cron" ]; then
cat > recebimento_routes.py <<'RECEOF'
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

import json
import os
import re
import threading
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
_tab_lock = threading.Lock()   # 2 telas abrindo juntas depois de reiniciar = 2 ALTER TABLE ao mesmo tempo = deadlock no Postgres
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
    with _tab_lock:
        if _tabelas_ok:
            return
        _garantir_tabelas()
        _tabelas_ok = True


def _garantir_tabelas():
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
        for tb, col in (("receb_notas", "pedido_xml"), ("receb_notas", "vinculo_por"), ("receb_itens", "xped")):
            if pg:
                # só altera se faltar a coluna (ALTER trava a tabela; e um erro engolido aqui estragava a transação inteira)
                tem = c.execute("SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?", (tb, col)).fetchone()
                if not tem:
                    c.execute(f"ALTER TABLE {tb} ADD COLUMN IF NOT EXISTS {col} TEXT")
            else:
                try:
                    c.execute(f"ALTER TABLE {tb} ADD COLUMN {col} TEXT")
                except Exception:
                    pass
        c.execute("CREATE TABLE IF NOT EXISTS receb_pc_cache (cnpj TEXT PRIMARY KEY, dados TEXT, em BIGINT)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS receb_vinculos (
                fornecedor_cnpj TEXT NOT NULL, cprod TEXT NOT NULL, sku TEXT NOT NULL,
                criado_por TEXT, criado_em BIGINT, PRIMARY KEY (fornecedor_cnpj, cprod)
            )""")


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
            "lote_xml": g(ras, "n:nLote") or lote_da_descricao(g(p, "n:xProd"))[0],
            "validade_xml": g(ras, "n:dVal") or lote_da_descricao(g(p, "n:xProd"))[1],
            "xped": _dig(g(p, "n:xPed")),
        })
    infcpl = g(inf.find("n:infAdic", NS), "n:infCpl")
    xpeds = sorted({it["xped"] for it in itens if it["xped"]})
    ped_txt = re.findall(r"pedido(?:\s+de\s+compra)?\D{0,12}?(\d{2,9})", infcpl, flags=re.I)
    return {
        "chave": chave, "numero": g(ide, "n:nNF"), "serie": g(ide, "n:serie"), "emissao": g(ide, "n:dhEmi")[:10],
        "fornecedor_cnpj": _dig(g(emit, "n:CNPJ") or g(emit, "n:CPF")), "fornecedor_nome": g(emit, "n:xNome"),
        "valor": float(g(tot, "n:vNF") or 0), "itens": itens,
        # nº do pedido de compra informado pelo fornecedor: campo xPed dos itens (certo) ou texto adicional (reserva)
        "pedido_xml": ",".join(xpeds) if xpeds else (ped_txt[0] if ped_txt else ""),
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
                        status, criado_em, origem, atualizado_em, pedido_xml) VALUES (?,?,?,?,?,?,?,?,'aguardando',?,?,?,?)""",
                  (nf["chave"], nf["numero"], nf["serie"], nf["emissao"], nf["fornecedor_cnpj"], nf["fornecedor_nome"],
                   nf["valor"], xml_txt, _agora(), origem, _agora(), nf["pedido_xml"] or None))
        nid = c.execute("SELECT id FROM receb_notas WHERE chave=?", (nf["chave"],)).fetchone()["id"]
        vinc = {v["cprod"]: v["sku"] for v in c.execute("SELECT cprod, sku FROM receb_vinculos WHERE fornecedor_cnpj=?", (nf["fornecedor_cnpj"],)).fetchall()}
        for it in nf["itens"]:
            sku = vinc.get(it["cprod"]) or _sku_existente(c, it["cprod"], it["ean"])
            c.execute("""INSERT INTO receb_itens (nota_id, n_item, cprod, ean, descricao, ncm, unidade, qtd_nf, valor_unit,
                            lote_xml, validade_xml, sku, lote, validade, xped) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (nid, it["n_item"], it["cprod"], it["ean"], it["descricao"], it["ncm"], it["unidade"], it["qtd_nf"],
                       it["valor_unit"], it["lote_xml"], it["validade_xml"], sku, it["lote_xml"] or None, it["validade_xml"] or None,
                       it["xped"] or None))
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


# ── pedidos de compra do fornecedor: cache rápido (memória + banco), atualiza em segundo plano ──
PC_FRESCO_S = int(os.environ.get("RECEB_PC_FRESCO_S", "120"))      # até 2 min: usa direto
PC_VELHO_MAX_S = 6 * 3600                                          # até 6 h: usa e atualiza em segundo plano
PC_DET_TTL_S = 30 * 60                                             # detalhe de cada pedido (itens) vale 30 min
_pc_det_cache: dict = {}        # pc_id -> (ts, total, dict pronto)
_pc_atualizando: set = set()
_pc_lock = threading.Lock()
_rl_lock = threading.Lock()
_rl_ultimo = [0.0]


def _ritmo():
    """No máximo ~3 chamadas/s ao Bling somando todas as threads (limite da API)."""
    with _rl_lock:
        espera = _rl_ultimo[0] + 0.34 - time.time()
        if espera > 0:
            time.sleep(espera)
        _rl_ultimo[0] = time.time()


def _pc_formatar(pc, det):
    return {"id": pc["id"], "numero": pc.get("numero"), "data": pc.get("data"), "prevista": pc.get("dataPrevista"),
            "total": pc.get("total"), "situacao": (pc.get("situacao") or {}).get("valor"),
            "itens": [{"sku": (i.get("produto") or {}).get("codigo"), "cprod": i.get("codigoFornecedor"),
                       "descricao": i.get("descricao"), "qtd": i.get("quantidade"),
                       "atendido": (i.get("notaFiscal") or {}).get("quantidade") or 0, "valor": i.get("valor")}
                      for i in det.get("itens") or []]}


def _buscar_pcs(cnpj: str) -> list | None:
    """Vai ao Bling: lista os pedidos em aberto/em andamento e só detalha os que mudaram (3 em paralelo, no ritmo da API)."""
    from concurrent.futures import ThreadPoolExecutor
    b = _bling()
    if not (b and cnpj):
        return None
    t0 = time.time()
    try:
        _ritmo()
        ct = b._get_json("/contatos", {"numeroDocumento": cnpj, "limite": 5}).get("data") or []
        ids = [x["id"] for x in ct if _dig(x.get("numeroDocumento")) == cnpj]
        lista = []
        for fid in ids[:2]:
            for sit in (0, 3):      # em aberto, em andamento
                _ritmo()
                lista += b._get_json("/pedidos/compras", {"idFornecedor": fid, "idsSituacoes[]": sit, "limite": 50}).get("data") or []
    except Exception as e:
        print(f"⚠️ recebimento: pedidos de compra do Bling falharam: {e}", flush=True)
        return None
    agora = time.time()

    def detalhe(pc):
        c0 = _pc_det_cache.get(pc["id"])
        if c0 and agora - c0[0] < PC_DET_TTL_S and str(c0[1]) == str(pc.get("total")):
            return c0[2]
        bb = _bling()
        _ritmo()
        det = (bb._get_json(f"/pedidos/compras/{pc['id']}") or {}).get("data") or {}
        f = _pc_formatar(pc, det)
        _pc_det_cache[pc["id"]] = (time.time(), pc.get("total"), f)
        return f

    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            out = list(ex.map(detalhe, lista))
    except Exception as e:
        print(f"⚠️ recebimento: detalhe dos pedidos de compra falhou: {e}", flush=True)
        return None
    print(f"[RECEB] pedidos de compra {cnpj}: {len(out)} em {time.time() - t0:.1f}s", flush=True)
    return out


def _pc_guardar(cnpj, out):
    _pc_cache[cnpj] = (time.time(), out)
    try:
        with db_session() as c:
            c.execute("DELETE FROM receb_pc_cache WHERE cnpj=?", (cnpj,))
            c.execute("INSERT INTO receb_pc_cache (cnpj, dados, em) VALUES (?,?,?)", (cnpj, json.dumps(out, ensure_ascii=False), _agora()))
    except Exception as e:
        print(f"⚠️ recebimento: cache de pedidos no banco: {e}", flush=True)


def _pc_do_banco(cnpj):
    try:
        with db_session() as c:
            r = c.execute("SELECT dados, em FROM receb_pc_cache WHERE cnpj=?", (cnpj,)).fetchone()
        if r:
            return (float(r["em"]), json.loads(r["dados"]))
    except Exception:
        pass
    return None


def _atualizar_em_segundo_plano(cnpj):
    with _pc_lock:
        if cnpj in _pc_atualizando:
            return
        _pc_atualizando.add(cnpj)

    def rodar():
        try:
            out = _buscar_pcs(cnpj)
            if out is not None:
                _pc_guardar(cnpj, out)
        finally:
            with _pc_lock:
                _pc_atualizando.discard(cnpj)
    threading.Thread(target=rodar, daemon=True).start()


def pedidos_compra_do_fornecedor(cnpj: str, fresco: bool = False) -> list:
    """Pedidos de compra abertos/em andamento do fornecedor (Bling), com itens.
    Rápido: até 2 min usa o que já tem; até 6 h usa o que tem e atualiza em segundo plano; fresco=True vai ao Bling na hora."""
    cnpj = _dig(cnpj)
    if not cnpj:
        return []
    ent = _pc_cache.get(cnpj)
    if not ent:
        ent = _pc_do_banco(cnpj)
        if ent:
            _pc_cache[cnpj] = ent
    if ent and not fresco:
        idade = time.time() - ent[0]
        if idade < PC_FRESCO_S:
            return ent[1]
        if idade < PC_VELHO_MAX_S:
            _atualizar_em_segundo_plano(cnpj)
            return ent[1]
    out = _buscar_pcs(cnpj)
    if out is None:
        return ent[1] if ent else []
    _pc_guardar(cnpj, out)
    return out


def preaquecer_pcs(cnpjs):
    """Abre a lista de notas -> já busca os pedidos dos fornecedores das notas em aberto (a nota abre na hora)."""
    for cnpj in {_dig(x) for x in cnpjs if x}:
        ent = _pc_cache.get(cnpj) or _pc_do_banco(cnpj)
        if not ent or time.time() - ent[0] >= PC_FRESCO_S:
            _atualizar_em_segundo_plano(cnpj)


def _xml_bling_por_chave(chave: str):
    """Nota de fornecedor já lançada no Bling como nota de entrada: acha pela chave e baixa o XML (só leitura)."""
    b = _bling()
    if not b:
        return None
    try:
        for params in ({"chaveAcesso": chave, "limite": 5}, {"tipo": 0, "chaveAcesso": chave, "limite": 5}):
            for n in b._get_json("/nfe", params).get("data") or []:
                if _dig(n.get("chaveAcesso")) != chave:
                    continue
                det = (b._get_json(f"/nfe/{n['id']}") or {}).get("data") or {}
                if det.get("xml"):
                    txt = b._download(det["xml"]).decode("utf-8", "ignore")
                    if "infNFe" in txt and chave in txt:
                        return txt
    except Exception as e:
        print(f"⚠️ recebimento: busca no Bling por chave falhou: {e}", flush=True)
    return None


# ───────────────────────── Meu Danfe (API v2) ─────────────────────────
MEUDANFE_BASE = os.environ.get("MEUDANFE_BASE", "https://api.meudanfe.com.br/v2")


def buscar_xml_por_chave(chave: str):
    """XML da NF pela chave no Meu Danfe: GET (grátis se já está na área); senão PUT /fd/add (R$0,03, busca na SEFAZ)
    e espera o XML aparecer (até ~20 s). Sem MEUDANFE_TOKEN no .env devolve None."""
    tok = (os.environ.get("MEUDANFE_TOKEN") or "").strip()
    if not tok:
        return None
    import requests
    h = {"Api-Key": tok}

    def pegar():
        r = requests.get(f"{MEUDANFE_BASE}/fd/get/xml/{chave}", headers=h, timeout=20)
        if r.status_code != 200:
            return None
        try:
            txt = (r.json() or {}).get("data") or ""
        except Exception:
            txt = r.text or ""
        return txt if "infNFe" in txt else None

    try:
        x = pegar()
        if x:
            return x
        r = requests.put(f"{MEUDANFE_BASE}/fd/add/{chave}", headers=h, timeout=60)
        print(f"[RECEB] meudanfe add {chave}: HTTP {r.status_code} {r.text[:160]}", flush=True)
        if r.status_code not in (200, 201):
            return None
        for _ in range(7):
            time.sleep(3)
            x = pegar()
            if x:
                return x
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
        if not it.get("lote_xml"):
            lt, vl = lote_da_descricao(it.get("descricao"))
            if lt:
                it["lote_xml"], it["validade_xml"], it["lote_origem"] = lt, it.get("validade_xml") or vl, "descricao"
        it["pc_qtd"] = float(it["pc_qtd"]) if it["pc_qtd"] is not None else None
    d["itens"] = itens
    d["motivos"] = [m for m in (r["motivos"] or "").split("\n") if m]
    if com_pc:
        pcs = pedidos_compra_do_fornecedor(r["fornecedor_cnpj"])
        d["pedidos_compra"] = [{k: v for k, v in pc.items() if k != "itens"} | {"n_itens": len(pc["itens"])} for pc in pcs]
        d["pedido_compra"] = next((pc for pc in pcs if pc["id"] == r["pedido_compra_id"]), None)
        try:
            _, sug = sugestoes_pedido(d, pcs)
            d["sugestoes_pc"] = [{k: v for k, v in x.items() if not k.startswith("_")} for x in sug[:6]]
        except Exception as e:
            print(f"⚠️ recebimento: sugestões falharam: {e}", flush=True)
            d["sugestoes_pc"] = []
    if com_pc:
        d["divergencias_pedido"] = _divergencias(d, conferencia=False)
        d["comparacao"] = _comparacao(d)
    d["permite_liberar"] = PERMITE_LIBERAR
    d["exigir_lote"] = EXIGIR_LOTE
    d["eh_gestor"] = eh_gestor()
    return d


PRECO_TOL = float(os.environ.get("RECEB_PRECO_TOL", "0.01"))   # R$ por unidade (só arredondamento)
PERMITE_LIBERAR = os.environ.get("RECEB_PERMITE_LIBERAR", "0") == "1"   # padrão: divergência NUNCA é liberada


def _brl(v, casas=2) -> str:
    """R$ no formato brasileiro: 8.370,84"""
    t = f"{float(v or 0):,.{casas}f}"
    return "R$ " + t.replace(",", "X").replace(".", ",").replace("X", ".")


def _comparacao(d: dict) -> dict | None:
    """NF x pedido, linha a linha (qtd, preço unitário, total da linha e diferença) + totais. Mostra DE ONDE vem a diferença."""
    pc = d.get("pedido_compra")
    if not pc:
        return None
    linhas, usados = [], set()
    for it in d["itens"]:
        pi = _item_pc(pc, it)
        q_nf, vu_nf = float(it["qtd_nf"] or 0), float(it.get("valor_unit") or 0)
        t_nf = round(q_nf * vu_nf, 2)
        if not pi:
            linhas.append({"n_item": it["n_item"], "descricao": it["descricao"], "sku": it.get("sku"), "qtd_nf": q_nf, "qtd_pc": None,
                           "vu_nf": vu_nf, "vu_pc": None, "tot_nf": t_nf, "tot_pc": 0.0, "dif": t_nf, "situacao": "fora_pedido"})
            continue
        usados.add(id(pi))
        q_pc, vu_pc = float(pi.get("qtd") or 0), float(pi.get("valor") or 0)
        t_pc = round(q_pc * vu_pc, 2)
        dif = round(t_nf - t_pc, 2)
        if abs(q_nf - q_pc) > 1e-6:
            sit = "quantidade"
        elif abs(vu_nf - vu_pc) > PRECO_TOL:
            sit = "preco"
        elif abs(dif) >= 0.005:
            sit = "centavos"          # preço unitário bate (até R$ 0,01), mas a linha difere no arredondamento
        else:
            sit = "ok"
        linhas.append({"n_item": it["n_item"], "descricao": it["descricao"], "sku": it.get("sku"), "qtd_nf": q_nf, "qtd_pc": q_pc,
                       "vu_nf": vu_nf, "vu_pc": vu_pc, "tot_nf": t_nf, "tot_pc": t_pc, "dif": dif, "situacao": sit})
    for pi in pc["itens"]:
        if id(pi) not in usados:
            q_pc, vu_pc = float(pi.get("qtd") or 0), float(pi.get("valor") or 0)
            t_pc = round(q_pc * vu_pc, 2)
            linhas.append({"n_item": None, "descricao": pi.get("descricao") or pi.get("sku") or "", "sku": pi.get("sku"), "qtd_nf": None,
                           "qtd_pc": q_pc, "vu_nf": None, "vu_pc": vu_pc, "tot_nf": 0.0, "tot_pc": t_pc, "dif": -t_pc, "situacao": "nao_veio"})
    tot_nf = round(sum(x["tot_nf"] for x in linhas), 2)
    tot_pc = round(sum(x["tot_pc"] for x in linhas), 2)
    return {"pedido": pc.get("numero"), "linhas": linhas, "tot_nf": tot_nf, "tot_pc": tot_pc, "dif": round(tot_nf - tot_pc, 2),
            "tol_total": 0.05, "tol_unit": PRECO_TOL}


_RX_LOTE = re.compile(r"\b(?:LT|LOTE|LOT)\s*[:.]?\s*([A-Z0-9][A-Z0-9/\-.]{2,24})", re.I)
_RX_VAL = re.compile(r"\b(?:VAL|VALIDADE|VENC|VCTO)\s*[:.]?\s*(\d{2})[/.-](\d{2})[/.-](\d{2,4})", re.I)


def lote_da_descricao(desc):
    """Fornecedor sem <rastro> que escreve 'LT: 20240410 - VAL: 01/01/2040' na descrição -> (lote, 'AAAA-MM-DD')."""
    t = str(desc or "")
    m, v = _RX_LOTE.search(t), _RX_VAL.search(t)
    lote = m.group(1).strip(" -.") if m else ""
    val = ""
    if v:
        d, mth, a = v.groups()
        a = ("20" + a) if len(a) == 2 else a
        val = f"{a}-{mth}-{d}"
    return lote, val


def _item_pc(pc, it):
    """Item do pedido que corresponde ao item da nota: SKU igual, ou código do fornecedor igual."""
    for x in pc["itens"]:
        if x["sku"] and it["sku"] and str(x["sku"]).upper() == str(it["sku"]).upper():
            return x
    for x in pc["itens"]:
        if x["cprod"] and it["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper():
            return x
    return None


def _preencher_skus_pelo_pedido(c, d: dict):
    """Com o pedido vinculado, item sem SKU pega o SKU do item do pedido de mesmo código do fornecedor (e aprende)."""
    pc = d.get("pedido_compra")
    if not pc:
        return False
    mudou = False
    for it in d["itens"]:
        if it["sku"] or not it["cprod"]:
            continue
        pi = next((x for x in pc["itens"] if x["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper() and x["sku"]), None)
        if pi:
            c.execute("UPDATE receb_itens SET sku=? WHERE id=?", (pi["sku"], it["id"]))
            c.execute("""INSERT INTO receb_vinculos (fornecedor_cnpj, cprod, sku, criado_por, criado_em) VALUES (?,?,?,?,?)
                         ON CONFLICT (fornecedor_cnpj, cprod) DO NOTHING""", (d["fornecedor_cnpj"], it["cprod"], pi["sku"], "pedido", _agora()))
            mudou = True
    return mudou


# ───────────────────────── casar NF x pedido de compra pelos ITENS ─────────────────────────
# Quando a nota NÃO traz o nº do pedido (xPed): compara item a item com os pedidos em aberto do fornecedor.
# Vínculo automático só se UM ÚNICO pedido bate 100% (todos os itens, quantidades, preços e total) e sem item ambíguo.
def _txt_norm(t):
    import unicodedata
    return unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()


def _sim(a, b):
    ta, tb = set(re.findall(r"[a-z0-9]{3,}", _txt_norm(a))), set(re.findall(r"[a-z0-9]{3,}", _txt_norm(b)))
    return len(ta & tb) / max(1, min(len(ta), len(tb))) if ta and tb else 0.0


def _parear(itens_nf, pc):
    up = lambda v: str(v or "").strip().upper()
    livres = list(range(len(pc["itens"])))
    pares = {}
    for i, it in enumerate(itens_nf):                         # 1) código: SKU nosso ou código do fornecedor
        for k in livres:
            x = pc["itens"][k]
            if (x.get("sku") and it.get("sku") and up(x["sku"]) == up(it["sku"])) or \
               (x.get("cprod") and it.get("cprod") and up(x["cprod"]) == up(it["cprod"])):
                pares[i] = (k, "codigo"); livres.remove(k); break
    for i, it in enumerate(itens_nf):                         # 2) sem código: mesma quantidade E mesmo preço
        if i in pares:
            continue
        cands = [k for k in livres if abs(float(pc["itens"][k].get("qtd") or 0) - float(it["qtd_nf"] or 0)) < 1e-6
                 and abs(float(pc["itens"][k].get("valor") or 0) - float(it.get("valor_unit") or 0)) <= PRECO_TOL]
        if not cands:
            continue
        cands.sort(key=lambda k: -_sim(it.get("descricao"), pc["itens"][k].get("descricao")))
        amb = len(cands) > 1 and _sim(it.get("descricao"), pc["itens"][cands[0]].get("descricao")) == \
            _sim(it.get("descricao"), pc["itens"][cands[1]].get("descricao"))
        pares[i] = (cands[0], "valores_ambiguo" if amb else "valores"); livres.remove(cands[0])
    batem, problemas = 0, []
    for i, it in enumerate(itens_nf):
        nome = (it.get("descricao") or "")[:38]
        if i not in pares:
            problemas.append(f"{nome}: não está no pedido"); continue
        x = pc["itens"][pares[i][0]]
        q_ok = abs(float(x.get("qtd") or 0) - float(it["qtd_nf"] or 0)) < 1e-6
        v_ok = abs(float(x.get("valor") or 0) - float(it.get("valor_unit") or 0)) <= PRECO_TOL
        if q_ok and v_ok:
            batem += 1
        else:
            problemas.append(f"{nome}: " + ("quantidade NF {:g} x pedido {:g}".format(float(it["qtd_nf"] or 0), float(x.get("qtd") or 0)) if not q_ok
                                            else "preço NF {} x pedido {}".format(_brl(it.get("valor_unit"), 4), _brl(x.get("valor"), 4))))
    for k in livres:
        x = pc["itens"][k]
        problemas.append(f"pedido tem {(x.get('descricao') or x.get('sku') or '')[:38]} ({float(x.get('qtd') or 0):g}) que não veio")
    tot_nf = round(sum(float(it["qtd_nf"] or 0) * float(it.get("valor_unit") or 0) for it in itens_nf), 2)
    tot_pc = round(sum(float(x.get("qtd") or 0) * float(x.get("valor") or 0) for x in pc["itens"]), 2)
    if abs(tot_nf - tot_pc) > 0.05:
        problemas.append(f"total NF {_brl(tot_nf)} x pedido {_brl(tot_pc)}")
    ambiguo = any(m == "valores_ambiguo" for _, m in pares.values())
    exato = not problemas and batem == len(itens_nf) and len(itens_nf) > 0
    return {"pares": pares, "batem": batem, "exato": exato, "ambiguo": ambiguo, "problemas": problemas,
            "por_codigo": sum(1 for _, m in pares.values() if m == "codigo")}


def sugestoes_pedido(d, pcs):
    """Ranking dos pedidos em aberto do fornecedor contra a nota + o que é vínculo 100% (auto)."""
    res = []
    for pc in pcs:
        r = _parear(d["itens"], pc)
        res.append({"id": pc["id"], "numero": pc.get("numero"), "data": pc.get("data"), "total": pc.get("total"),
                    "n_itens": len(pc["itens"]), "batem": r["batem"], "itens_nf": len(d["itens"]), "exato": r["exato"],
                    "ambiguo": r["ambiguo"], "por_codigo": r["por_codigo"], "problemas": r["problemas"][:5], "_r": r, "_pc": pc})
    res.sort(key=lambda x: (not x["exato"], -x["batem"], len(x["problemas"])))
    exatos = [x for x in res if x["exato"] and not x["ambiguo"]]
    auto = exatos[0] if len([x for x in res if x["exato"]]) == 1 and exatos else None
    return auto, res


def _aprender_skus(c, d, sug):
    """Pedido vinculado pelos itens: item da nota sem SKU herda o SKU do item do pedido que casou (e grava o vínculo)."""
    pc, r = sug["_pc"], sug["_r"]
    for i, (k, metodo) in r["pares"].items():
        it, x = d["itens"][i], pc["itens"][k]
        if it.get("sku") or not x.get("sku") or metodo == "valores_ambiguo":
            continue
        c.execute("UPDATE receb_itens SET sku=? WHERE id=?", (x["sku"], it["id"]))
        if it.get("cprod"):
            c.execute("""INSERT INTO receb_vinculos (fornecedor_cnpj, cprod, sku, criado_por, criado_em) VALUES (?,?,?,?,?)
                         ON CONFLICT (fornecedor_cnpj, cprod) DO NOTHING""", (d["fornecedor_cnpj"], it["cprod"], x["sku"], "itens", _agora()))


def _divergencias(d: dict, conferencia: bool = True) -> list:
    """conferencia=False: só NF x pedido (preço, quantidade, itens) — mostrado na tela antes de bipar."""
    m = []
    pc = d.get("pedido_compra")
    usados = set()
    for it in d["itens"]:
        nome = it["descricao"][:40]
        if not it["sku"]:
            m.append(f"Item {it['n_item']} ({nome}): sem SKU vinculado")
        if conferencia and it["qtd_conferida"] < it["qtd_nf"]:
            m.append(f"Item {it['n_item']} ({nome}): FALTOU — NF {it['qtd_nf']:g}, conferido {it['qtd_conferida']:g}")
        elif conferencia and it["qtd_conferida"] > it["qtd_nf"]:
            m.append(f"Item {it['n_item']} ({nome}): SOBROU — NF {it['qtd_nf']:g}, conferido {it['qtd_conferida']:g}")
        if conferencia and EXIGIR_LOTE and (not (it["lote"] or "").strip() or not (it["validade"] or "").strip()):
            m.append(f"Item {it['n_item']} ({nome}): sem lote/validade")
        if pc:
            pi = _item_pc(pc, it)
            if not pi:
                m.append(f"Item {it['n_item']} ({nome}): NÃO está no pedido de compra {pc['numero']}")
            else:
                usados.add(id(pi))
                qpc = float(pi["qtd"] or 0)
                if abs(it["qtd_nf"] - qpc) > 1e-6:
                    m.append(f"Item {it['n_item']} ({nome}): QUANTIDADE não bate — NF {it['qtd_nf']:g} x pedido {qpc:g}")
                vpc, vnf = float(pi.get("valor") or 0), float(it.get("valor_unit") or 0)
                if abs(vnf - vpc) > PRECO_TOL:
                    m.append(f"Item {it['n_item']} ({nome}): VALOR não bate — NF {_brl(vnf, 4)} x pedido {_brl(vpc, 4)}")
    if pc:
        for pi in pc["itens"]:
            if id(pi) not in usados:
                m.append(f"Item do pedido {pc['numero']} NÃO veio na nota: {(pi.get('descricao') or pi.get('sku') or '')[:40]} ({float(pi.get('qtd') or 0):g})")
        tot_nf = round(sum(it["qtd_nf"] * float(it.get("valor_unit") or 0) for it in d["itens"]), 2)
        tot_pc = round(sum(float(pi.get("qtd") or 0) * float(pi.get("valor") or 0) for pi in pc["itens"]), 2)
        if abs(tot_nf - tot_pc) > 0.05:
            cmp_ = _comparacao(d) or {}
            origem = [f"item {x['n_item']}" for x in cmp_.get("linhas", []) if x["situacao"] == "centavos"]
            m.append(f"TOTAL dos produtos não bate — NF {_brl(tot_nf)} x pedido {_brl(tot_pc)} (diferença {_brl(tot_nf - tot_pc)})"
                     + (f"; vem dos centavos em: {', '.join(origem)}" if origem else ""))
    if not pc:
        m.append("Nenhum pedido de compra vinculado"
                 + (f" (a nota diz pedido {d['pedido_xml']}, que não está em aberto para esse fornecedor no Bling)" if d.get("pedido_xml") else ""))
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
    xml = _xml_bling_por_chave(chave)
    if xml:
        try:
            return jsonify({"success": True, **_importar_xml(xml, "bling")})
        except Exception as e:
            print(f"⚠️ recebimento: XML do Bling inválido: {e}", flush=True)
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
        # contagem de itens de todas as notas numa consulta só (antes era 1 consulta por nota)
        cont = {int(x["nota_id"]): (int(x["n"]), int(x["ok"])) for x in c.execute(
            "SELECT nota_id, COUNT(*) AS n, COALESCE(SUM(CASE WHEN qtd_conferida>=qtd_nf THEN 1 ELSE 0 END),0) AS ok "
            "FROM receb_itens GROUP BY nota_id").fetchall()}
        out = []
        for r in rows:
            d = dict(r); d["valor"] = float(d["valor"] or 0)
            d["n_motivos"] = len([m for m in (r["motivos"] or "").split("\n") if m]); d.pop("motivos", None)
            d["n_itens"], d["itens_ok"] = cont.get(int(r["id"]), (0, 0))
            out.append(d)
    preaquecer_pcs([d["fornecedor_cnpj"] for d in out if d["status"] in ("aguardando", "conferindo")])
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
    if request.args.get("fresco"):
        with db_session() as c:
            r0 = c.execute("SELECT fornecedor_cnpj FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if r0:
            pedidos_compra_do_fornecedor(r0["fornecedor_cnpj"], fresco=True)
    with db_session() as c:
        d = _nota_dict(c, nid)
        if not d:
            return jsonify({"error": "Nota não encontrada."}), 404
        if d["status"] == "aguardando":
            c.execute("UPDATE receb_notas SET status='conferindo', atualizado_em=? WHERE id=?", (_agora(), nid)); d["status"] = "conferindo"
        # vínculo AUTOMÁTICO só quando a nota traz o nº do pedido (xPed) e ele é desse fornecedor: 100%, sem chute
        if not d["pedido_compra_id"] and d.get("pedido_xml"):
            nums = [x.strip().lstrip("0") for x in str(d["pedido_xml"]).split(",") if x.strip()]
            pc = next((p for p in (d.get("pedidos_compra") or []) if str(p.get("numero") or "").lstrip("0") in nums), None)
            if pc:
                c.execute("UPDATE receb_notas SET pedido_compra_id=?, pedido_compra_numero=?, vinculo_por='xml' WHERE id=?",
                          (pc["id"], str(pc["numero"]), nid))
                d = _nota_dict(c, nid)
        # sem nº de pedido na nota: vínculo AUTOMÁTICO só se um único pedido em aberto bate 100% item a item
        if not d["pedido_compra_id"] and d["status"] in ("aguardando", "conferindo"):
            pcs = pedidos_compra_do_fornecedor(d["fornecedor_cnpj"])
            auto, _ = sugestoes_pedido(d, pcs) if pcs else (None, [])
            if auto:
                c.execute("UPDATE receb_notas SET pedido_compra_id=?, pedido_compra_numero=?, vinculo_por='itens', atualizado_em=? WHERE id=?",
                          (auto["id"], str(auto["numero"]), _agora(), nid))
                _aprender_skus(c, d, auto)
                print(f"[RECEB] nota {d['numero']} vinculada pelos itens ao pedido {auto['numero']}", flush=True)
                d = _nota_dict(c, nid)
        if _preencher_skus_pelo_pedido(c, d):
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
            c.execute("UPDATE receb_notas SET pedido_compra_id=?, pedido_compra_numero=?, vinculo_por=?, atualizado_em=? WHERE id=?",
                      (pc["id"], str(pc["numero"]), "manual:" + _quem(), _agora(), nid))
            d0 = _nota_dict(c, nid, com_pc=False)
            r0 = _parear(d0["itens"], pc)
            _aprender_skus(c, d0, {"_pc": pc, "_r": {"pares": {i: v for i, v in r0["pares"].items()
                                                              if abs(float(pc["itens"][v[0]].get("qtd") or 0) - float(d0["itens"][i]["qtd_nf"] or 0)) < 1e-6}}})
        else:
            c.execute("UPDATE receb_notas SET pedido_compra_id=NULL, pedido_compra_numero=NULL, atualizado_em=? WHERE id=?", (_agora(), nid))
        d = _nota_dict(c, nid)
        if _preencher_skus_pelo_pedido(c, d):
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
        r0 = c.execute("SELECT fornecedor_cnpj FROM receb_notas WHERE id=?", (nid,)).fetchone()
    if r0:
        pedidos_compra_do_fornecedor(r0["fornecedor_cnpj"], fresco=True)   # decide com o pedido como está AGORA no Bling
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
    if not PERMITE_LIBERAR:
        return jsonify({"error": "Nota com divergência NÃO pode ser liberada. Corrija o pedido de compra no Bling "
                                 "(ou peça carta de correção/nova NF ao fornecedor) e clique em Reabrir para conferir de novo."}), 403
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
        r = c.execute("SELECT status, fornecedor_cnpj FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Nota não encontrada."}), 404
        _pc_cache.pop(_dig(r["fornecedor_cnpj"]), None)     # pedido pode ter sido corrigido no Bling: relê
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
RECEOF
cat > receb_email_sync.py <<'RECEOF'
#!/usr/bin/env python3
"""
Lê a caixa de e-mail onde os fornecedores mandam XML (ex.: financeiro@lhvmed.com.br) e importa toda NF-e
para o RECEBIMENTO do WMS (status "aguardando"). Roda por cron a cada 10 min. Só lê; não apaga, não move.

.env:
    RECEB_IMAP_HOST=email-ssl.com.br      (Locaweb)   RECEB_IMAP_PORT=993
    RECEB_IMAP_USER=financeiro@lhvmed.com.br
    RECEB_IMAP_PASS=...
    RECEB_IMAP_PASTA=INBOX                 RECEB_IMAP_DIAS=30   (quantos dias olhar pra trás na 1ª vez)

    venv/bin/python receb_email_sync.py          # importa o que ainda não foi lido
    venv/bin/python receb_email_sync.py --dry    # só mostra
"""
from __future__ import annotations

import email
import imaplib
import io
import os
import sys
import time
import zipfile
from datetime import date, timedelta
from email.header import decode_header

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

from db import db_session
import recebimento_routes as rr


def _dec(s):
    if not s:
        return ""
    out = []
    for txt, enc in decode_header(s):
        out.append(txt.decode(enc or "utf-8", "ignore") if isinstance(txt, bytes) else txt)
    return "".join(out)


def xmls_do_email(msg) -> list[tuple[str, str]]:
    """[(nome, conteudo)] de anexos .xml (inclusive dentro de .zip) que parecem NF-e."""
    out = []
    for part in msg.walk():
        nome = _dec(part.get_filename() or "")
        if not nome:
            continue
        dados = part.get_payload(decode=True) or b""
        if nome.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(dados)) as z:
                    for n in z.namelist():
                        if n.lower().endswith(".xml"):
                            out.append((n, z.read(n).decode("utf-8", "ignore")))
            except Exception:
                pass
        elif nome.lower().endswith(".xml"):
            out.append((nome, dados.decode("utf-8", "ignore")))
    return [(n, t) for n, t in out if "infNFe" in t and "<emit>" in t]


def main():
    dry = "--dry" in sys.argv
    host = os.environ.get("RECEB_IMAP_HOST", "email-ssl.com.br")
    port = int(os.environ.get("RECEB_IMAP_PORT", "993"))
    user, pwd = os.environ.get("RECEB_IMAP_USER"), os.environ.get("RECEB_IMAP_PASS")
    pasta = os.environ.get("RECEB_IMAP_PASTA", "INBOX")
    dias = int(os.environ.get("RECEB_IMAP_DIAS", "30"))
    if not user or not pwd:
        print("Configure RECEB_IMAP_USER e RECEB_IMAP_PASS no .env"); sys.exit(2)

    rr.garantir_tabelas()
    with db_session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS receb_email_uids (uid TEXT PRIMARY KEY, lido_em BIGINT)")
        lidos = {r["uid"] for r in c.execute("SELECT uid FROM receb_email_uids").fetchall()}

    m = imaplib.IMAP4_SSL(host, port)
    m.login(user, pwd)
    m.select(pasta, readonly=True)
    desde = (date.today() - timedelta(days=dias)).strftime("%d-%b-%Y")
    st, dados = m.uid("search", None, f'(SINCE {desde})')
    uids = [u.decode() for u in (dados[0].split() if dados and dados[0] else [])]
    novos = [u for u in uids if u not in lidos]
    print(f">> {len(uids)} e-mails desde {desde}, {len(novos)} ainda não lidos", flush=True)
    importadas = 0
    for u in novos:
        st, raw = m.uid("fetch", u, "(BODY.PEEK[])")
        if st != "OK" or not raw or not raw[0]:
            continue
        msg = email.message_from_bytes(raw[0][1])
        xmls = xmls_do_email(msg)
        for nome, txt in xmls:
            try:
                if dry:
                    nf = rr.parse_nfe(txt); print(f"   [dry] {nome}: NF {nf['numero']} {nf['fornecedor_nome']} {nf['valor']}")
                else:
                    r = rr._importar_xml(txt, "email")
                    if not r["ja_existia"]:
                        importadas += 1
                        print(f"   + {nome} -> nota #{r['id']}  ({_dec(msg.get('From'))[:50]})", flush=True)
            except Exception as e:
                print(f"   ! {nome}: {e}", flush=True)
        if not dry:
            with db_session() as c:
                c.execute("INSERT INTO receb_email_uids (uid, lido_em) VALUES (?,?) ON CONFLICT (uid) DO NOTHING", (u, int(time.time())))
    m.logout()
    print(f">> {importadas} nota(s) nova(s) no recebimento.", flush=True)


if __name__ == "__main__":
    main()
RECEOF
python3 -m py_compile recebimento_routes.py receb_email_sync.py && echo ">> recebimento_routes.py / receb_email_sync.py ok"

python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "recebimento_routes" in s:
    print(">> app.py ja tinha o recebimento_routes (ok)")
else:
    linha = '    from recebimento_routes import recebimento_routes as recebimento_bp\n    app.register_blueprint(recebimento_bp, url_prefix="/api")\n'
    ancoras = ['    app.register_blueprint(picking_bp, url_prefix="/api")\n',
               '    app.register_blueprint(devolucoes_bp, url_prefix="/api")\n',
               '    app.register_blueprint(mapa_bp, url_prefix="/api")\n',
               "    app.register_blueprint(api_routes)\n"]
    for a in ancoras:
        if a in s:
            open(f + ".bak_recebimento", "w", encoding="utf-8").write(s)
            open(f, "w", encoding="utf-8").write(s.replace(a, a + linha, 1))
            print(">> app.py patchado (backup em app.py.bak_recebimento)")
            break
    else:
        print("!! Nao achei onde registrar. Adicione dentro de create_app():"); print(linha); raise SystemExit(1)
PYEOF
python3 -m py_compile app.py && echo ">> app.py COMPILOU"
echo ""
echo "Reinicie: sudo systemctl restart wms-russia ; teste: curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/recebimento/resumo  (deve dar 401)"
echo "REVERTER: cp app.py.bak_recebimento app.py && sudo systemctl restart wms-russia   (as tabelas receb_* ficam, nao atrapalham)"
echo "E-mail: coloque RECEB_IMAP_USER / RECEB_IMAP_PASS no .env e rode: venv/bin/python receb_email_sync.py --dry"
exit 0
fi

# --cron
[ -f receb_email_sync.py ] || { echo "Rode antes: bash instalar_recebimento.sh"; exit 1; }
DIR="$PWD"; LOG="$HOME/nexuswms/receb_email.log"; mkdir -p "$HOME/nexuswms" "$HOME/backups_wms"
crontab -l > "$HOME/backups_wms/crontab_antes_receb_$(date +%Y%m%d_%H%M%S).txt" 2>/dev/null || true
if crontab -l 2>/dev/null | grep -q "receb_email_sync.py"; then echo ">> cron ja existe (ok)"; exit 0; fi
( crontab -l 2>/dev/null
  echo "*/10 * * * * cd $DIR && flock -n /tmp/receb_email.lock venv/bin/python receb_email_sync.py 2>&1 | grep -v -i warn >> $LOG"
) | crontab -
echo ">> cron ligado: e-mail lido a cada 10 min. Log: $LOG"
