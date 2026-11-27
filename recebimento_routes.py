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
import datetime
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
        _completar_validades()
        _tabelas_ok = True
    try:
        _vigia_ligar()          # notas esperando ser importadas no Bling (depois de reiniciar o serviço)
    except Exception:
        pass


def _completar_validades():
    """Nota aberta com item sem validade cuja descrição tem 'VAL: SET/29' / 'VAL: 09/2029' (antes não era lido): completa."""
    try:
        with db_session() as c:
            rows = c.execute("SELECT i.id, i.descricao FROM receb_itens i JOIN receb_notas n ON n.id=i.nota_id "
                             "WHERE n.status IN ('aguardando','conferindo','recusada') AND COALESCE(i.validade,'')='' "
                             "AND COALESCE(i.validade_xml,'')=''").fetchall()
            for r in rows:
                vl = lote_da_descricao(r["descricao"])[1]
                if vl:
                    c.execute("UPDATE receb_itens SET validade_xml=?, validade=? WHERE id=?", (vl, vl, r["id"]))
    except Exception as e:
        print(f"⚠️ completar validades: {e}", flush=True)


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
        for tb, col in (("receb_notas", "pedido_xml"), ("receb_notas", "vinculo_por"), ("receb_itens", "xped"),
                        ("receb_notas", "entrada_etapa"), ("receb_notas", "entrada_em"), ("receb_notas", "entrada_por"),
                        ("receb_notas", "entrada_msg"), ("receb_notas", "entrada_erro"), ("receb_notas", "entrada_resumo"),
                        ("receb_itens", "qtd_bipada"), ("receb_itens", "ajuste_por"), ("receb_itens", "fabricacao"),
                        ("receb_itens", "serie"), ("receb_itens", "validade_indet")):   # nº de série e validade indeterminada (Biel 25/11)
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
        c.execute(f"""CREATE TABLE IF NOT EXISTS receb_entrada_log (id {pk}, nota_id BIGINT NOT NULL, acao TEXT, ok INTEGER, http INTEGER,
                      mensagem TEXT, resposta TEXT, enviado TEXT, por TEXT, em BIGINT)""")
        c.execute("CREATE TABLE IF NOT EXISTS receb_pc_cache (cnpj TEXT PRIMARY KEY, dados TEXT, em BIGINT)")
        c.execute("CREATE TABLE IF NOT EXISTS receb_kits (sku_key TEXT PRIMARY KEY, dados TEXT, em BIGINT)")
        c.execute("""CREATE TABLE IF NOT EXISTS receb_etq (codigo TEXT PRIMARY KEY, nota_id BIGINT NOT NULL, item_id BIGINT NOT NULL,
                      sku TEXT, mult INTEGER NOT NULL DEFAULT 1, seq INTEGER, lote TEXT, criado_por TEXT, criado_em BIGINT,
                      bipado_por TEXT, bipado_em BIGINT, cancelado INTEGER NOT NULL DEFAULT 0)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_receb_etq_item ON receb_etq (item_id)")
        # rastreio da unidade (18/11): uso atual da etiqueta em cada etapa + histórico de tudo que aconteceu com ela
        c.execute("""CREATE TABLE IF NOT EXISTS etq_uso (codigo TEXT NOT NULL, etapa TEXT NOT NULL, sku TEXT, pedido TEXT,
                      quem TEXT, em BIGINT, dia TEXT, tk TEXT, PRIMARY KEY (codigo, etapa))""")
        c.execute(f"""CREATE TABLE IF NOT EXISTS etq_evento (id {pk}, codigo TEXT, etapa TEXT, acao TEXT, sku TEXT, pedido TEXT,
                      quem TEXT, em BIGINT, detalhe TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_etq_evento_em ON etq_evento (em)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_etq_evento_cod ON etq_evento (codigo)")
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
        # O Bling IGNORA idsSituacoes[] aqui e devolve do mais antigo para o mais novo (DORJA x pedido 3304, 23/11: o fornecedor
        # tinha mais de 50 pedidos e o novo nunca entrava). Percorre todas as páginas e filtra aqui: 0 em aberto, 3 em andamento.
        for fid in ids[:3]:
            for pag in range(1, 41):
                _ritmo()
                d = b._get_json("/pedidos/compras", {"idFornecedor": fid, "limite": 100, "pagina": pag}).get("data") or []
                lista += [p for p in d if (p.get("situacao") or {}).get("valor") in (0, 3, "0", "3")]
                if len(d) < 100:
                    break
        lista.sort(key=lambda p: str(p.get("data") or ""), reverse=True)
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
def _txt_endereco(pp, pos):
    try:                                       # mapa montável (16/11): nomes e códigos vêm do layout
        import mapa_layout
        return mapa_layout.endereco(pp, pos)
    except Exception:
        pass
    pp, pos = int(pp), int(pos)
    if pp > 100:
        return f"C{pp - 100}-F{pos}", f"Cesta {pp - 100} · Fileira {pos}"
    if pp in (31, 32):
        return f"PPV{pp - 30}-P{pos}", f"Porta Palet Vet {pp - 30} · Posição P{pos}"
    if pp in (51, 52):
        return f"REF{pp - 50}-P{pos}", "Refrigerador"
    return f"PP{pp}-P{pos}", f"Porta Palet {pp} · Posição P{pos}"


def _enderecos(c, itens):
    """Onde guardar: posições do SKU no Mapa do galpão (mapa_localizacao)."""
    skus = sorted({str(it.get("sku") or "").strip().lower() for it in itens if it.get("sku")})
    por = {}
    if skus:
        try:
            with db_session() as c2:        # sessão própria: se a tabela do mapa não existir, não estraga a transação da nota
                for r in c2.execute(f"SELECT sku_key, porta_palet, posicao FROM mapa_localizacao WHERE sku_key IN ({','.join('?' * len(skus))}) "
                                    "ORDER BY porta_palet, posicao", tuple(skus)).fetchall():
                    cod, txt = _txt_endereco(r["porta_palet"], r["posicao"])
                    por.setdefault(r["sku_key"], []).append({"codigo": cod, "texto": txt})
        except Exception:
            pass
    for it in itens:
        it["enderecos"] = por.get(str(it.get("sku") or "").strip().lower(), [])


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
        _num_bip(it)
        if not it.get("lote_xml"):
            lt, vl = lote_da_descricao(it.get("descricao"))
            if lt:
                it["lote_xml"], it["validade_xml"], it["lote_origem"] = lt, it.get("validade_xml") or vl, "descricao"
        if not it.get("validade_xml"):
            vl = lote_da_descricao(it.get("descricao"))[1]
            if vl:
                it["validade_xml"] = vl
        it["pc_qtd"] = float(it["pc_qtd"]) if it["pc_qtd"] is not None else None
    d["itens"] = itens
    _enderecos(c, itens)
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
    d["tol_total"] = TOL_TOTAL
    d["digitar_so_gestor"] = DIGITAR_SO_GESTOR
    try:
        _etq_info(c, nid, d)
    except Exception as e:                  # tabela nova ainda não criada etc.: a tela segue no jeito antigo
        print(f"⚠️ recebimento: etiquetas únicas: {e}", flush=True)
    d["eh_gestor"] = eh_gestor()
    return d


PRECO_TOL = float(os.environ.get("RECEB_PRECO_TOL", "0.01"))   # R$ por unidade (só arredondamento)
# Biel 12/11: diferença de até R$ 0,10 (arredondamento) PASSA — na linha e no total dos produtos; acima disso recusa
TOL_TOTAL = float(os.environ.get("RECEB_TOL_TOTAL", "0.10"))
# Biel 12/11 (tarde): os produtos não vêm com etiqueta nossa -> etiqueta impressa ao abrir a nota e conferência por botão
# ("Está tudo certo" / quantidade diferente). Digitar volta a ser de todos (RECEB_DIGITAR_SO_GESTOR=1 trava de novo).
DIGITAR_SO_GESTOR = os.environ.get("RECEB_DIGITAR_SO_GESTOR", "0") == "1"
PERMITE_LIBERAR = os.environ.get("RECEB_PERMITE_LIBERAR", "0") == "1"   # padrão: divergência NUNCA é liberada


def _brl(v, casas=2) -> str:
    """R$ no formato brasileiro: 8.370,84"""
    t = f"{float(v or 0):,.{casas}f}"
    return "R$ " + t.replace(",", "X").replace(".", ",").replace("X", ".")


def _up(v):
    return str(v or "").strip().upper()


def _mesmo_prod(a, b):
    return bool((a.get("sku") and b.get("sku") and _up(a["sku"]) == _up(b["sku"])) or
                (a.get("cprod") and b.get("cprod") and _up(a["cprod"]) == _up(b["cprod"])))


def _desc_key(t):
    """Descrição normalizada para comparar NF x pedido (sem acento, só letras e números)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def _pc_por_descricao(it, pc_itens, grupos_nf=None):
    """Item do pedido com a MESMA descrição do item da nota — só quando é única dos dois lados (sem chute).
    27/11: Nalgon manda cProd diferente do 'código do fornecedor' do pedido, mas a descrição é idêntica."""
    k = _desc_key(it.get("descricao"))
    if len(k) < 6:
        return None
    def bate(a, b):   # igual, ou uma começa com a outra (pedido com observação depois do nome)
        return a == b or (len(a) >= 12 and len(b) >= 12 and (a.startswith(b) or b.startswith(a)))
    cands = [x for x in pc_itens if bate(_desc_key(x.get("descricao")), k)]
    if len(cands) != 1:
        return None
    kp = _desc_key(cands[0].get("descricao"))
    if grupos_nf is not None and sum(1 for g in grupos_nf if bate(kp, _desc_key(g["nf"][0].get("descricao")))) != 1:
        return None
    return cands[0]


def _grupos(itens_nf, pc):
    """Junta por PRODUTO: o mesmo produto pode vir em várias linhas da nota (um lote por linha) e/ou do pedido.
    -> (grupos [{nf: [...], pc: [...]}], itens do pedido que não casaram com nenhum produto da nota)"""
    grupos = []
    for it in itens_nf:
        g = next((g for g in grupos if any(_mesmo_prod(it, o) for o in g["nf"])), None)
        if g:
            g["nf"].append(it)
        else:
            grupos.append({"nf": [it], "pc": []})
    sobras = []
    for x in (pc or {}).get("itens") or []:
        g = (next((g for g in grupos if any(x.get("sku") and o.get("sku") and _up(x["sku"]) == _up(o["sku"]) for o in g["nf"])), None)
             or next((g for g in grupos if any(x.get("cprod") and o.get("cprod") and _up(x["cprod"]) == _up(o["cprod"]) for o in g["nf"])), None))
        if not g:   # 3) mesma DESCRIÇÃO, única dos dois lados (27/11)
            g = next((g for g in grupos if not g["pc"] and not any(o.get("sku") for o in g["nf"])
                      and _pc_por_descricao(g["nf"][0], (pc or {}).get("itens") or [], grupos) is x), None)
        if g:
            g["pc"].append(x)
        else:
            sobras.append(x)
    return grupos, sobras


def _conta_grupo(g):
    """Números do grupo: quantidades e totais somados, diferença e a situação (ok / centavos / quantidade / preco)."""
    q_nf = sum(float(i["qtd_nf"] or 0) for i in g["nf"])
    t_nf = round(sum(round(float(i["qtd_nf"] or 0) * float(i.get("valor_unit") or 0), 2) for i in g["nf"]), 2)
    q_pc = sum(float(x.get("qtd") or 0) for x in g["pc"])
    t_pc = round(sum(round(float(x.get("qtd") or 0) * float(x.get("valor") or 0), 2) for x in g["pc"]), 2)
    precos_pc = [float(x.get("valor") or 0) for x in g["pc"]]
    dif = round(t_nf - t_pc, 2)
    # Biel 12/11: vale o DINHEIRO — até R$ 0,10 de diferença no produto passa (preço com mais casas, arredondamento)
    preco_ruim = [] if abs(dif) <= TOL_TOTAL + 1e-9 else \
        [i for i in g["nf"] if not any(abs(float(i.get("valor_unit") or 0) - v) <= PRECO_TOL for v in precos_pc)]
    if abs(q_nf - q_pc) > 1e-6:
        sit = "quantidade"
    elif abs(dif) > TOL_TOTAL + 1e-9:
        sit = "preco"
    elif abs(dif) >= 0.005:
        sit = "centavos"          # arredondamento até R$ 0,10: aceito
    else:
        sit = "ok"
    return {"q_nf": q_nf, "t_nf": t_nf, "q_pc": q_pc, "t_pc": t_pc, "dif": dif, "sit": sit, "preco_ruim": preco_ruim,
            "vu_pc": (precos_pc[0] if len(set(precos_pc)) == 1 else (t_pc / q_pc if q_pc else None)) if precos_pc else None}


def _comparacao(d: dict) -> dict | None:
    """NF x pedido por PRODUTO (linhas do mesmo produto somadas: lote em várias linhas) + totais. Mostra DE ONDE vem a diferença."""
    pc = d.get("pedido_compra")
    if not pc:
        return None
    linhas = []
    grupos, sobras = _grupos(d["itens"], pc)
    for g in grupos:
        it0 = g["nf"][0]
        nums = [i["n_item"] for i in g["nf"]]
        vus = {float(i.get("valor_unit") or 0) for i in g["nf"]}
        desc = it0["descricao"] + (f" ({len(g['nf'])} linhas na nota)" if len(g["nf"]) > 1 else "")
        if not g["pc"]:
            for i in g["nf"]:
                q, vu = float(i["qtd_nf"] or 0), float(i.get("valor_unit") or 0)
                linhas.append({"n_item": i["n_item"], "n_itens": [i["n_item"]], "descricao": i["descricao"], "sku": i.get("sku"), "qtd_nf": q,
                               "qtd_pc": None, "vu_nf": vu, "vu_pc": None, "tot_nf": round(q * vu, 2), "tot_pc": 0.0, "dif": round(q * vu, 2),
                               "situacao": "fora_pedido"})
            continue
        n = _conta_grupo(g)
        linhas.append({"n_item": nums[0], "n_itens": nums, "descricao": desc, "sku": it0.get("sku"), "qtd_nf": n["q_nf"], "qtd_pc": n["q_pc"],
                       "vu_nf": (next(iter(vus)) if len(vus) == 1 else (n["t_nf"] / n["q_nf"] if n["q_nf"] else 0)), "vu_pc": n["vu_pc"],
                       "tot_nf": n["t_nf"], "tot_pc": n["t_pc"], "dif": n["dif"], "situacao": n["sit"],
                       "linhas_pc": len(g["pc"])})
    for pi in sobras:
        q_pc, vu_pc = float(pi.get("qtd") or 0), float(pi.get("valor") or 0)
        t_pc = round(q_pc * vu_pc, 2)
        linhas.append({"n_item": None, "n_itens": [], "descricao": pi.get("descricao") or pi.get("sku") or "", "sku": pi.get("sku"), "qtd_nf": None,
                       "qtd_pc": q_pc, "vu_nf": None, "vu_pc": vu_pc, "tot_nf": 0.0, "tot_pc": t_pc, "dif": -t_pc, "situacao": "nao_veio"})
    tot_nf = round(sum(x["tot_nf"] for x in linhas), 2)
    tot_pc = round(sum(x["tot_pc"] for x in linhas), 2)
    return {"pedido": pc.get("numero"), "linhas": linhas, "tot_nf": tot_nf, "tot_pc": tot_pc, "dif": round(tot_nf - tot_pc, 2),
            "tol_total": TOL_TOTAL, "tol_unit": PRECO_TOL}


_RX_LOTE = re.compile(r"\b(?:LT|LOTE|LOT)\s*[:.]?\s*([A-Z0-9][A-Z0-9/\-.]{2,24})", re.I)
_RX_VAL = re.compile(r"\b(?:VAL|VALIDADE|VENC|VCTO)\s*[:.]?\s*(\d{2})[/.-](\d{2})[/.-](\d{2,4})", re.I)
# Biel 18/11: "VAL: SET/29", "Val: OUT/29" (Dermafreeze, NF 13003) e "VAL: 09/2029" não eram lidos -> etiqueta sem validade
_MESES = {"JAN": 1, "FEV": 2, "FEB": 2, "MAR": 3, "ABR": 4, "APR": 4, "MAI": 5, "MAY": 5, "JUN": 6, "JUL": 7, "AGO": 8, "AUG": 8,
          "SET": 9, "SEP": 9, "OUT": 10, "OCT": 10, "NOV": 11, "DEZ": 12, "DEC": 12}
_RX_VAL_MES = re.compile(r"\b(?:VAL|VALIDADE|VENC|VCTO)\s*[:.]?\s*(JAN|FEV|FEB|MAR|ABR|APR|MAI|MAY|JUN|JUL|AGO|AUG|SET|SEP|OUT|OCT|NOV|DEZ|DEC)[A-Z]*"
                         r"\s*[/.\- ]\s*(\d{2}|\d{4})\b", re.I)
_RX_VAL_MA = re.compile(r"\b(?:VAL|VALIDADE|VENC|VCTO)\s*[:.]?\s*(\d{1,2})[/.-](\d{4}|\d{2})(?![/.\-]?\d)", re.I)


def _fim_do_mes(ano, mes):
    import calendar
    return f"{ano:04d}-{mes:02d}-{calendar.monthrange(ano, mes)[1]:02d}"


def lote_da_descricao(desc):
    """Fornecedor sem <rastro> que escreve a validade na descrição -> (lote, 'AAAA-MM-DD').
    'VAL: 01/01/2040' (dia/mês/ano), 'VAL: SET/29' ou 'Val: OUT/2029' (mês por extenso: último dia do mês), 'VAL: 09/2029'."""
    t = str(desc or "")
    m = _RX_LOTE.search(t)
    lote = m.group(1).strip(" -.") if m else ""
    val = ""
    v = _RX_VAL.search(t)
    if v:
        d, mth, a = v.groups()
        a = ("20" + a) if len(a) == 2 else a
        val = f"{a}-{mth}-{d}"
    else:
        v = _RX_VAL_MES.search(t)
        if v:
            a = int(v.group(2)); a = a + 2000 if a < 100 else a
            val = _fim_do_mes(a, _MESES[v.group(1).upper()[:3]])
        else:
            v = _RX_VAL_MA.search(t)
            if v and 1 <= int(v.group(1)) <= 12:
                a = int(v.group(2)); a = a + 2000 if a < 100 else a
                val = _fim_do_mes(a, int(v.group(1)))
    if lote and val:            # 'Lote: 151414/SQ VAL...' -> o lote não leva o pedaço da validade
        lote = re.split(r"\s*\b(?:VAL|VALIDADE|VENC|VCTO)\b", lote, flags=re.I)[0].strip(" -.")
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
    grupos, _ = _grupos(d["itens"], pc)
    for it in d["itens"]:
        if it["sku"]:
            continue
        pi = None
        if it["cprod"]:
            pi = next((x for x in pc["itens"] if x["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper() and x["sku"]), None)
        origem = "pedido"
        if not pi:   # 27/11: mesma descrição (única) no pedido
            pi = _pc_por_descricao(it, pc["itens"], grupos)
            origem = "descricao"
            if pi and not pi.get("sku"):
                pi = None
        if pi:
            c.execute("UPDATE receb_itens SET sku=? WHERE id=?", (pi["sku"], it["id"]))
            it["sku"] = pi["sku"]
            if it["cprod"]:
                c.execute("""INSERT INTO receb_vinculos (fornecedor_cnpj, cprod, sku, criado_por, criado_em) VALUES (?,?,?,?,?)
                             ON CONFLICT (fornecedor_cnpj, cprod) DO NOTHING""", (d["fornecedor_cnpj"], it["cprod"], pi["sku"], origem, _agora()))
            print(f"[RECEB] item {it['n_item']} ({str(it.get('descricao'))[:30]}) herdou SKU {pi['sku']} do pedido por {origem}", flush=True)
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
    """Casa a nota com UM pedido. 1) por código (SKU/código do fornecedor), juntando o mesmo produto em várias linhas;
    2) item sem código casado: mesma quantidade E mesmo preço num item livre do pedido."""
    grupos, sobras = _grupos(itens_nf, pc)
    idx_pc = {id(x): k for k, x in enumerate(pc["itens"])}
    idx_nf = {id(it): i for i, it in enumerate(itens_nf)}
    pares, sem, ok_grupos = {}, [], []
    for g in grupos:
        if g["pc"]:
            for it in g["nf"]:
                pares[idx_nf[id(it)]] = (idx_pc[id(g["pc"][0])], "codigo")
            ok_grupos.append(g)
        else:
            sem.extend(g["nf"])
    livres = [idx_pc[id(x)] for x in sobras]
    for it in sem:                                            # 2) sem código: mesma quantidade E mesmo preço
        cands = [k for k in livres if abs(float(pc["itens"][k].get("qtd") or 0) - float(it["qtd_nf"] or 0)) < 1e-6
                 and abs(float(pc["itens"][k].get("valor") or 0) - float(it.get("valor_unit") or 0)) <= PRECO_TOL]
        if not cands:
            continue
        cands.sort(key=lambda k: -_sim(it.get("descricao"), pc["itens"][k].get("descricao")))
        amb = len(cands) > 1 and _sim(it.get("descricao"), pc["itens"][cands[0]].get("descricao")) == \
            _sim(it.get("descricao"), pc["itens"][cands[1]].get("descricao"))
        pares[idx_nf[id(it)]] = (cands[0], "valores_ambiguo" if amb else "valores")
        livres.remove(cands[0])
        ok_grupos.append({"nf": [it], "pc": [pc["itens"][cands[0]]]})
    batem, problemas = 0, []
    casados = {id(it) for g in ok_grupos for it in g["nf"]}
    for it in itens_nf:
        if id(it) not in casados:
            problemas.append(f"{(it.get('descricao') or '')[:38]}: não está no pedido")
    for g in ok_grupos:
        n = _conta_grupo(g)
        nome = (g["nf"][0].get("descricao") or "")[:38]
        if n["sit"] in ("ok", "centavos"):
            batem += len(g["nf"])
        elif n["sit"] == "quantidade":
            problemas.append(f"{nome}: quantidade NF {n['q_nf']:g} x pedido {n['q_pc']:g}")
        else:
            problemas.append(f"{nome}: preço NF {_brl(g['nf'][0].get('valor_unit'), 4)} x pedido {_brl(n['vu_pc'], 4) if n['vu_pc'] is not None else '-'}")
    for k in livres:
        x = pc["itens"][k]
        problemas.append(f"pedido tem {(x.get('descricao') or x.get('sku') or '')[:38]} ({float(x.get('qtd') or 0):g}) que não veio")
    tot_nf = round(sum(round(float(it["qtd_nf"] or 0) * float(it.get("valor_unit") or 0), 2) for it in itens_nf), 2)
    tot_pc = round(sum(round(float(x.get("qtd") or 0) * float(x.get("valor") or 0), 2) for x in pc["itens"]), 2)
    if abs(tot_nf - tot_pc) > TOL_TOTAL + 1e-9:
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
            m.append(f"Item {it['n_item']} ({nome}): sem SKU vinculado (código do fornecedor na NF: {it.get('cprod') or '-'}"
                     f"{', EAN ' + it['ean'] if it.get('ean') else ''}) — toque no item e escolha o SKU")
        if conferencia and it["qtd_conferida"] < it["qtd_nf"]:
            m.append(f"Item {it['n_item']} ({nome}): FALTOU — NF {it['qtd_nf']:g}, conferido {it['qtd_conferida']:g}")
        elif conferencia and it["qtd_conferida"] > it["qtd_nf"]:
            m.append(f"Item {it['n_item']} ({nome}): SOBROU — NF {it['qtd_nf']:g}, conferido {it['qtd_conferida']:g}")
        if conferencia and EXIGIR_LOTE and (not _tem_lote(it) or not _tem_validade(it)):
            m.append(f"Item {it['n_item']} ({nome}): sem lote/série ou validade (se não vence, marque 'validade indeterminada')")
    if pc:
        grupos, sobras = _grupos(d["itens"], pc)
        for g in grupos:
            nomes = "Item " + "+".join(str(i["n_item"]) for i in g["nf"]) + f" ({g['nf'][0]['descricao'][:40]})"
            if not g["pc"]:
                for i in g["nf"]:
                    m.append(f"Item {i['n_item']} ({i['descricao'][:40]}): NÃO está no pedido de compra {pc['numero']}")
                continue
            n = _conta_grupo(g)
            if n["sit"] == "quantidade":
                m.append(f"{nomes}: QUANTIDADE não bate — NF {n['q_nf']:g} x pedido {n['q_pc']:g}")
            for i in n["preco_ruim"]:
                m.append(f"Item {i['n_item']} ({i['descricao'][:40]}): VALOR não bate — NF {_brl(i.get('valor_unit'), 4)} x pedido "
                         f"{_brl(n['vu_pc'], 4) if n['vu_pc'] is not None else '-'}")
            if n["sit"] == "preco" and not n["preco_ruim"]:
                m.append(f"{nomes}: VALOR não bate — diferença {_brl(n['dif'])} (aceito até {_brl(TOL_TOTAL)})")
        for pi in sobras:
            m.append(f"Item do pedido {pc['numero']} NÃO veio na nota: {(pi.get('descricao') or pi.get('sku') or '')[:40]} ({float(pi.get('qtd') or 0):g})")
        tot_nf = round(sum(round(it["qtd_nf"] * float(it.get("valor_unit") or 0), 2) for it in d["itens"]), 2)
        tot_pc = round(sum(round(float(pi.get("qtd") or 0) * float(pi.get("valor") or 0), 2) for pi in pc["itens"]), 2)
        if abs(tot_nf - tot_pc) > TOL_TOTAL + 1e-9:
            cmp_ = _comparacao(d) or {}
            origem = ["item " + "+".join(str(n) for n in x["n_itens"]) for x in cmp_.get("linhas", []) if x["situacao"] in ("centavos", "preco") and abs(x["dif"]) >= 0.005]
            m.append(f"TOTAL dos produtos não bate — NF {_brl(tot_nf)} x pedido {_brl(tot_pc)} (diferença {_brl(tot_nf - tot_pc)}; aceito até {_brl(TOL_TOTAL)})"
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


def _num_bip(it):
    """qtd_bipada (número) e manual = conferido além do que foi bipado (digitado/+ na tela)."""
    try:
        it["qtd_bipada"] = float(it.get("qtd_bipada") or 0)
    except (TypeError, ValueError):
        it["qtd_bipada"] = 0.0
    it["qtd_manual"] = max(0.0, float(it.get("qtd_conferida") or 0) - it["qtd_bipada"])
    return it


def _cod_norm(v):
    """Compara códigos ignorando caixa e zeros à esquerda de código numérico (EAN-13 x GTIN-14)."""
    k = str(v or "").strip().upper()
    return k.lstrip("0") or k if k.isdigit() else k


def _acha_item(itens, codigo: str):
    """Mesmo produto pode vir em mais de uma linha (um lote por linha): vai para a 1ª linha que AINDA FALTA;
    se todas já estão completas, cai na 1ª (e aparece SOBROU)."""
    k = _cod_norm(codigo)
    for campo in ("sku", "ean", "cprod"):
        achados = [it for it in itens if it.get(campo) and _cod_norm(it[campo]) == k]
        if achados:
            return next((it for it in achados if float(it.get("qtd_conferida") or 0) < float(it.get("qtd_nf") or 0)), achados[0])
    return None


# ───────────────────────── etiqueta única por unidade (Biel 16/11) ─────────────────────────
# Cada etiqueta impressa no recebimento tem um código só dela: QR "SKU*LOTE*U1234ABC" (sem lote: "SKU**U1234ABC").
# Biel 18/11: "o cara imprime todas as etiquetas dos produtos, funciona como está hoje, só que no fim tem que bipar todas
# essas etiquetas para confirmar a unidade que entrou". Cada código conta UMA vez (kit conta as unidades do kit).
# Biel 18/11 16:22: "a opção de Está tudo certo tem que sair, não pode mais ser assim: só bipando e colando".
# RECEB_ETQ_UNICA: "estrito" (padrão) = em TODA nota aberta a conferência é só colando e bipando a etiqueta de cada unidade
#                  (sem "Está tudo certo"/"Quantidade diferente"/bip do produto; supervisor/admin ajusta na mão);
#                  "fim" = confere como antes e bipa as etiquetas no fim para fechar; "0" = desligado (sem código nas etiquetas).
ETQ_UNICA = (os.environ.get("RECEB_ETQ_UNICA", "estrito").strip().lower() or "estrito")
if ETQ_UNICA in ("nota", "1", "true", "sim", "on", "todas"):
    ETQ_UNICA = "estrito"
ETQ_DESLIGADA = ETQ_UNICA in ("0", "false", "nao", "não", "off")
_RE_ETQ = re.compile(r"^U[0-9A-HJKMNP-TV-Z]{7}$")
_ALFA = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"          # sem I, L, O, U: não confunde lendo o número impresso


def _novo_cod():
    import secrets
    return "U" + "".join(secrets.choice(_ALFA) for _ in range(7))


def _parse_etq(lido):
    """'SKU*LOTE*U1234ABC' -> (sku, lote, codigo); 'SKU*LOTE' -> (sku, lote, None); outro -> (lido, None, None)."""
    t = str(lido or "").strip()
    if "*" not in t:
        return t, None, None
    p = t.split("*")
    return p[0].strip(), (p[1].strip() or None) if len(p) > 1 else None, (p[2].strip().upper() or None) if len(p) > 2 else None


def _tem_etq(c, nid) -> bool:
    return not ETQ_DESLIGADA and bool(c.execute("SELECT 1 FROM receb_etq WHERE nota_id=? AND cancelado=0 LIMIT 1", (nid,)).fetchone())


def _etq_ativa(c, nid) -> bool:
    """Modo estrito: em toda nota a conferência é só bipando a etiqueta (sem etiqueta impressa ainda: imprima)."""
    return ETQ_UNICA == "estrito"


def _unid_bipadas(c, iid) -> int:
    r = c.execute("SELECT COALESCE(SUM(mult),0) AS u FROM receb_etq WHERE item_id=? AND cancelado=0 AND bipado_em IS NOT NULL", (iid,)).fetchone()
    return int(r["u"] or 0)


def _confirmar_por_etiqueta(c, iid):
    """Modo fim: a etiqueta bipada prova que a unidade está ali; o conferido nunca fica menor que o que foi bipado."""
    if ETQ_UNICA == "estrito":
        return _recontar(c, iid)
    u = _unid_bipadas(c, iid)
    it = c.execute("SELECT qtd_conferida FROM receb_itens WHERE id=?", (iid,)).fetchone()
    if u > float(it["qtd_conferida"] or 0):
        c.execute("UPDATE receb_itens SET qtd_conferida=? WHERE id=?", (float(u), iid))
    return u


def _faltam_etiquetas(c, nid):
    """Itens da nota com etiqueta em que as etiquetas bipadas não batem com o conferido -> [{item_id, descricao, conferido, bipadas}]."""
    if not _tem_etq(c, nid):
        return []
    out = []
    for it in c.execute("SELECT id, n_item, descricao, qtd_conferida FROM receb_itens WHERE nota_id=? ORDER BY n_item", (nid,)).fetchall():
        conf = int(round(float(it["qtd_conferida"] or 0)))
        tem = c.execute("SELECT 1 FROM receb_etq WHERE item_id=? AND cancelado=0 LIMIT 1", (it["id"],)).fetchone()
        b = _unid_bipadas(c, it["id"])
        if conf <= 0 and b == 0:
            continue
        if not tem or b != conf:
            out.append({"item_id": it["id"], "n_item": it["n_item"], "descricao": it["descricao"], "conferido": conf, "bipadas": b, "sem_etiqueta": not tem})
    return out


def _etq_info(c, nid, d):
    """Na nota: modo e, por item, quantas etiquetas e quantas já bipadas."""
    d["etq_modo"] = ETQ_UNICA
    d["etq_suporte"] = not ETQ_DESLIGADA
    d["etq_unica"] = _etq_ativa(c, nid)                 # estrito: só bipando a etiqueta
    d["etq_confirmar"] = ETQ_UNICA == "fim" and _tem_etq(c, nid)    # fim: fecha só com todas as etiquetas bipadas
    rows = c.execute("SELECT item_id, COUNT(*) AS n, SUM(mult) AS u, SUM(CASE WHEN bipado_em IS NOT NULL THEN 1 ELSE 0 END) AS nb, "
                     "SUM(CASE WHEN bipado_em IS NOT NULL THEN mult ELSE 0 END) AS ub FROM receb_etq "
                     "WHERE nota_id=? AND cancelado=0 GROUP BY item_id", (nid,)).fetchall()
    por = {int(r["item_id"]): r for r in rows}
    for it in d.get("itens") or []:
        r = por.get(int(it["id"]))
        it["etq_n"] = int(r["n"] or 0) if r else 0
        it["etq_unid"] = int(r["u"] or 0) if r else 0
        it["etq_bipadas"] = int(r["nb"] or 0) if r else 0
        it["etq_unid_bipadas"] = int(r["ub"] or 0) if r else 0


def _recontar(c, iid):
    """Item conferido = soma das unidades das etiquetas bipadas (cada código uma vez)."""
    u = c.execute("SELECT COALESCE(SUM(mult),0) AS u FROM receb_etq WHERE item_id=? AND cancelado=0 AND bipado_em IS NOT NULL", (iid,)).fetchone()["u"]
    u = float(u or 0)
    c.execute("UPDATE receb_itens SET qtd_conferida=?, qtd_bipada=? WHERE id=?", (u, str(u), iid))
    return u


def _tem_validade(it) -> bool:
    """Validade informada OU marcada como indeterminada (produto que não vence: instrumental, etc.)."""
    return bool(str(it.get("validade") or "").strip()) or str(it.get("validade_indet") or "") in ("1", "true", "True")


def _tem_lote(it) -> bool:
    """Lote OU nº de série (produto seriado não tem lote: a série identifica a unidade)."""
    return bool(str(it.get("lote") or "").strip()) or bool(str(it.get("serie") or "").strip())


def _lote_serie_sets(d, sets, args):
    """Campos lote/série/validade/indeterminada vindos da tela -> SETs do UPDATE. Série sem lote vira o lote (o Bling e a
    etiqueta usam o lote como identificador). Validade digitada desliga 'indeterminada' e vice-versa."""
    if d.get("serie") is not None and "serie" in d:
        sr = str(d["serie"] or "").strip()[:60] or None
        sets.append("serie=?"); args.append(sr)
        if sr and not str(d.get("lote") or "").strip():
            sets.append("lote=?"); args.append(sr)
    if d.get("validade_indet") in (1, True, "1", "true"):
        sets.append("validade_indet=?"); args.append("1")
        sets.append("validade=?"); args.append(None)
    elif d.get("validade"):
        sets.append("validade_indet=?"); args.append(None)


def _item_resp(c, iid):
    novo = dict(c.execute("SELECT * FROM receb_itens WHERE id=?", (iid,)).fetchone())
    novo["qtd_nf"] = float(novo["qtd_nf"] or 0); novo["qtd_conferida"] = float(novo["qtd_conferida"] or 0)
    _num_bip(novo)
    r = c.execute("SELECT COUNT(*) AS n, SUM(mult) AS u, SUM(CASE WHEN bipado_em IS NOT NULL THEN 1 ELSE 0 END) AS nb FROM receb_etq "
                  "WHERE item_id=? AND cancelado=0", (iid,)).fetchone()
    novo["etq_n"], novo["etq_unid"], novo["etq_bipadas"] = int(r["n"] or 0), int(r["u"] or 0), int(r["nb"] or 0)
    novo["etq_unid_bipadas"] = int(novo["qtd_conferida"])
    return novo


@recebimento_routes.post("/recebimento/notas/<int:nid>/etiquetas/codigos")
@login_required
def etiquetas_codigos(nid):
    """Códigos únicos para imprimir. Corpo: {itens: [{item_id, grupos: [{sku, mult, qtd}]}]}  (grupo = etiqueta da unidade
    ou do kit). Devolve os mesmos códigos se já existirem (reimprimir não cria outros). Etiqueta além da quantidade da nota
    só quando todas as dele já foram bipadas (sobrou produto) ou com supervisor/admin."""
    if ETQ_DESLIGADA:
        return jsonify({"error": "Etiqueta com código único está desligada (RECEB_ETQ_UNICA=0)."}), 409
    d = request.get_json(silent=True) or {}
    pedidos = d.get("itens") or []
    garantir_tabelas()
    out = {}
    with db_session() as c:
        nt = c.execute("SELECT status FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not nt:
            return jsonify({"error": "Nota não encontrada."}), 404
        aberta = nt["status"] not in ("conferida", "lancada")
        for ped in pedidos:
            iid = int(ped.get("item_id") or 0)
            it = c.execute("SELECT * FROM receb_itens WHERE id=? AND nota_id=?", (iid, nid)).fetchone()
            if not it:
                return jsonify({"error": f"Item {iid} não é desta nota."}), 400
            grupos = []
            for g in ped.get("grupos") or []:
                sku = str(g.get("sku") or "").strip()
                try:
                    mult, q = max(1, int(g.get("mult") or 1)), max(0, int(g.get("qtd") or 0))
                except (TypeError, ValueError):
                    return jsonify({"error": "Quantidade de etiquetas inválida."}), 400
                if sku and q:
                    grupos.append((sku, mult, q))
            exist = [dict(r) for r in c.execute("SELECT * FROM receb_etq WHERE item_id=? AND cancelado=0 ORDER BY seq", (iid,)).fetchall()]
            usados, plano = set(), []
            for sku, mult, q in grupos:
                mesmos = [e for e in exist if e["sku"] == sku and int(e["mult"]) == mult and e["codigo"] not in usados][:q]
                usados.update(e["codigo"] for e in mesmos)
                plano.append((sku, mult, q, mesmos))
            bip_fora = [e for e in exist if e["codigo"] not in usados and e["bipado_em"]]
            unid = sum(m * q for _, m, q, _ in plano) + sum(int(e["mult"]) for e in bip_fora)
            nf = int(-(-float(it["qtd_nf"] or 0) // 1))
            pendentes = [e for e in exist if not e["bipado_em"]]
            if unid > nf and not eh_gestor() and pendentes:
                return jsonify({"error": f"{it['descricao'][:40]}: a nota tem {nf} unidade(s) e isso daria {unid}. Etiqueta a mais só "
                                         "depois de bipar todas as que já saíram (sobrou produto), ou com o supervisor.",
                                "tipo": "acima_da_nota", "item_id": iid}), 403
            seq = int(c.execute("SELECT COALESCE(MAX(seq),0) AS m FROM receb_etq WHERE item_id=?", (iid,)).fetchone()["m"] or 0)
            lote = (it["lote"] or it["lote_xml"] or None)
            res = []
            for sku, mult, q, mesmos in plano:
                cods = [{"codigo": e["codigo"], "bipado": bool(e["bipado_em"])} for e in mesmos]
                while len(cods) < q:
                    seq += 1
                    for _ in range(5):
                        cod = _novo_cod()
                        if not c.execute("SELECT 1 FROM receb_etq WHERE codigo=?", (cod,)).fetchone():
                            break
                    c.execute("INSERT INTO receb_etq (codigo, nota_id, item_id, sku, mult, seq, lote, criado_por, criado_em) VALUES (?,?,?,?,?,?,?,?,?)",
                              (cod, nid, iid, sku, mult, seq, lote, _quem(), _agora()))
                    cods.append({"codigo": cod, "bipado": False})
                res.append({"sku": sku, "mult": mult, "codigos": cods})
            # etiqueta que não foi bipada e saiu desta impressão (trocou unidade <-> kit, diminuiu): deixa de valer
            for e in exist:
                if e["codigo"] not in usados and not e["bipado_em"]:
                    c.execute("UPDATE receb_etq SET cancelado=1 WHERE codigo=?", (e["codigo"],))
            if aberta and ETQ_UNICA == "estrito":    # estrito: o item conferido é só o que foi bipado (apaga "Está tudo certo")
                _recontar(c, iid)
            out[str(iid)] = {"grupos": res}
        c.execute("UPDATE receb_notas SET atualizado_em=? WHERE id=?", (_agora(), nid))
    return jsonify({"success": True, "itens": out})


@recebimento_routes.post("/recebimento/notas/<int:nid>/etiquetas/desbipar")
@login_required
def etiquetas_desbipar(nid):
    """Desfaz o último bip de etiqueta do item (ou o código informado)."""
    d = request.get_json(silent=True) or {}
    garantir_tabelas()
    with db_session() as c:
        n = c.execute("SELECT status FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not n:
            return jsonify({"error": "Nota não encontrada."}), 404
        if n["status"] in ("conferida", "lancada"):
            return jsonify({"error": "Nota já conferida — reabra para alterar."}), 409
        if d.get("codigo"):
            e = c.execute("SELECT * FROM receb_etq WHERE codigo=? AND nota_id=?", (str(d["codigo"]).upper(), nid)).fetchone()
        else:
            e = c.execute("SELECT * FROM receb_etq WHERE item_id=? AND nota_id=? AND bipado_em IS NOT NULL AND cancelado=0 "
                          "ORDER BY bipado_em DESC, seq DESC LIMIT 1", (int(d.get("item_id") or 0), nid)).fetchone()
        if not e or not e["bipado_em"]:
            return jsonify({"error": "Nenhuma etiqueta bipada para desfazer."}), 404
        c.execute("UPDATE receb_etq SET bipado_em=NULL, bipado_por=NULL WHERE codigo=?", (e["codigo"],))
        _evento(c, e["codigo"], "recebimento", "soltou", e["sku"], None, _quem(), f"desfez a conferência (era de {e['bipado_por'] or '?'})")
        if ETQ_UNICA == "estrito":
            _recontar(c, e["item_id"])
        novo = _item_resp(c, e["item_id"])
    return jsonify({"success": True, "item": novo, "codigo": e["codigo"], "falta": novo["qtd_nf"] - novo["qtd_conferida"]})


def _bipar_etq(c, nid, nota, sku, lote, cod):
    """Bip de etiqueta com código único. -> (json, http)."""
    e = c.execute("SELECT * FROM receb_etq WHERE codigo=?", (cod,)).fetchone()
    if not e:
        _evento(c, cod, "recebimento", "desconhecida", sku, nota.get("numero"), _quem(), "etiqueta que o sistema não imprimiu")
        return {"success": False, "tipo": "desconhecida", "error": f"Etiqueta {cod} não existe no sistema (etiqueta de teste ou de outra impressão). Use as etiquetas impressas para esta nota."}, 404
    if int(e["nota_id"]) != int(nid):
        o = c.execute("SELECT numero FROM receb_notas WHERE id=?", (e["nota_id"],)).fetchone()
        _evento(c, cod, "recebimento", "outra_nota", sku, nota.get("numero"), _quem(), f"etiqueta da NF {o['numero'] if o else '?'}")
        return {"success": False, "tipo": "outra_nota", "error": f"Essa etiqueta é da NF {o['numero'] if o else '?'}, não desta."}, 409
    if int(e["cancelado"] or 0):
        _evento(c, cod, "recebimento", "substituida", sku, nota.get("numero"), _quem(), "etiqueta trocada por outra impressão")
        return {"success": False, "tipo": "cancelada", "error": "Essa etiqueta foi substituída por outra impressão e não vale mais. Use a etiqueta nova."}, 409
    if e["bipado_em"]:
        hora = datetime.datetime.fromtimestamp(int(e["bipado_em"])).strftime("%H:%M")
        novo = _item_resp(c, e["item_id"])
        _evento(c, cod, "recebimento", "repetida", sku, nota.get("numero"), _quem(), f"já bipada por {e['bipado_por'] or '?'} às {hora}")
        return {"success": False, "tipo": "repetida", "item": novo, "falta": novo["qtd_nf"] - novo["qtd_conferida"],
                "error": f"Bipada por {e['bipado_por'] or '?'} às {hora}. Não contou de novo."}, 409
    c.execute("UPDATE receb_etq SET bipado_em=?, bipado_por=? WHERE codigo=? AND bipado_em IS NULL", (_agora(), _quem(), cod))
    _confirmar_por_etiqueta(c, e["item_id"])
    it = c.execute("SELECT lote FROM receb_itens WHERE id=?", (e["item_id"],)).fetchone()
    if lote and not (it and it["lote"]):
        c.execute("UPDATE receb_itens SET lote=? WHERE id=?", (str(lote)[:60], e["item_id"]))
    c.execute("UPDATE receb_notas SET status='conferindo', atualizado_em=? WHERE id=? AND status IN ('aguardando','recusada','conferindo')", (_agora(), nid))
    novo = _item_resp(c, e["item_id"])
    falta = novo["qtd_nf"] - novo["qtd_conferida"]
    return {"success": True, "item": novo, "falta": falta, "excedeu": falta < 0, "mult": int(e["mult"] or 1), "codigo": cod,
            "precisa_lote": EXIGIR_LOTE and not (_tem_lote(novo) and _tem_validade(novo))}, 200


@recebimento_routes.post("/recebimento/notas/<int:nid>/bipar")
@login_required
def bipar(nid):
    d = request.get_json(silent=True) or {}
    codigo = str(d.get("codigo") or "").strip()
    cod_unico = None
    if "*" in codigo:                       # etiqueta do WMS: QR "SKU*LOTE" ou "SKU*LOTE*U1234ABC"
        codigo, lote_etq, cod_unico = _parse_etq(codigo)
        d.setdefault("lote", lote_etq)
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
        if cod_unico:
            js, http = _bipar_etq(c, nid, nota, codigo, d.get("lote"), cod_unico)
            return jsonify(js), http
        if _etq_ativa(c, nid):
            return jsonify({"success": False, "tipo": "precisa_etiqueta",
                            "error": "Bipe a etiqueta do WMS colada no produto (cada unidade tem um código próprio). "
                                     "Código do fabricante e etiqueta antiga não contam."}), 409
        it = _acha_item(nota["itens"], codigo)
        if not it:
            return jsonify({"success": False, "error": f"{codigo} não está nesta nota. Confira o produto ou vincule o SKU no item certo.", "nao_na_nota": True}), 404
        sets, args = ["qtd_conferida = COALESCE(qtd_conferida,0) + ?",
                      "qtd_bipada = CAST(COALESCE(CAST(NULLIF(qtd_bipada,'') AS NUMERIC),0) + ? AS TEXT)"], [qtd, qtd]
        if d.get("lote"):
            sets.append("lote=?"); args.append(str(d["lote"]).strip()[:60])
        if d.get("validade"):
            sets.append("validade=?"); args.append(str(d["validade"]).strip()[:10])
        _lote_serie_sets(d, sets, args)
        c.execute(f"UPDATE receb_itens SET {', '.join(sets)} WHERE id=?", tuple(args) + (it["id"],))
        c.execute("UPDATE receb_notas SET status='conferindo', atualizado_em=? WHERE id=? AND status IN ('aguardando','recusada','conferindo')", (_agora(), nid))
        novo = dict(c.execute("SELECT * FROM receb_itens WHERE id=?", (it["id"],)).fetchone())
    novo["qtd_nf"] = float(novo["qtd_nf"] or 0); novo["qtd_conferida"] = float(novo["qtd_conferida"] or 0)
    _num_bip(novo)
    falta = novo["qtd_nf"] - novo["qtd_conferida"]
    return jsonify({"success": True, "item": novo, "falta": falta, "excedeu": falta < 0,
                    "precisa_lote": EXIGIR_LOTE and not (_tem_lote(novo) and _tem_validade(novo))})


# ───────────────────────── rastreio da unidade (Biel 18/11) ─────────────────────────
# "vai colar em cada uma uma etiqueta que diferencie uma da outra pra gente conseguir pegar caca dos caras":
# a etiqueta única do recebimento acompanha a unidade. Separação, Expedição e Full reservam o código ANTES de contar
# (POST /recebimento/unidade/usar). Na mesma etapa e no mesmo dia, a mesma etiqueta não conta duas vezes: o bip repetido
# para "completar" o pedido é recusado e fica registrado (quem tentou, quando, de quem era). Unidade que voltou para a
# prateleira e sai de novo em OUTRO dia passa (fica no histórico como "de novo"). O supervisor libera uma etiqueta no
# Rastrear. Etiqueta que o sistema não imprimiu (ou substituída) conta, mas fica registrada como ocorrência.
ETAPAS = {"recebimento": "Recebimento", "separacao": "Separação", "expedicao": "Expedição", "full": "Full"}
OCORRENCIAS = ("repetida", "desconhecida", "substituida", "outra_nota", "liberou")
_RE_ETQ_NO_TEXTO = re.compile(r"(?:^|[*#\s])(U[0-9A-HJKMNP-TV-Z]{7})\s*$")


def _utc(ts):
    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).replace(tzinfo=None)


def _dia_sp(ts=None) -> str:
    """Data em Brasília (o 'mesmo dia' da regra)."""
    t = _utc(ts if ts is not None else _agora()) - datetime.timedelta(hours=3)
    return t.strftime("%Y-%m-%d")


def _hora_sp(ts) -> str:
    try:
        t = _utc(int(ts)) - datetime.timedelta(hours=3)
        return t.strftime("%H:%M") if t.strftime("%Y-%m-%d") == _dia_sp() else t.strftime("%d/%m %H:%M")
    except Exception:
        return "?"


def _cod_do_texto(t) -> str | None:
    """'SKU*LOTE*U1234ABC', '#U1234ABC' ou 'U1234ABC' -> 'U1234ABC'."""
    t = str(t or "").strip().upper()
    if _RE_ETQ.match(t):
        return t
    m = _RE_ETQ_NO_TEXTO.search(t)
    return m.group(1) if m else None


def _evento(c, codigo, etapa, acao, sku=None, pedido=None, quem=None, detalhe=None):
    """Histórico da etiqueta. Nunca derruba o bip: se falhar, segue."""
    try:
        c.execute("INSERT INTO etq_evento (codigo, etapa, acao, sku, pedido, quem, em, detalhe) VALUES (?,?,?,?,?,?,?,?)",
                  (codigo, etapa, acao, (str(sku)[:80] if sku else None), (str(pedido)[:60] if pedido else None), quem, _agora(),
                   (str(detalhe)[:300] if detalhe else None)))
    except Exception as ex:
        print(f"⚠️ etq_evento: {ex}", flush=True)


def _txt_uso(r) -> str:
    ped = f" no pedido {r['pedido']}" if r.get("pedido") else ""
    return f"{r.get('quem') or '?'} às {_hora_sp(r.get('em'))}{ped}"


@recebimento_routes.post("/recebimento/unidade/usar")
@login_required
def unidade_usar():
    """Reserva a etiqueta da unidade nesta etapa antes de contar. Corpo: {codigo, etapa, sku, pedido?}.
    200 = pode contar (aviso quando a etiqueta não é do sistema / foi substituída / sai de novo em outro dia);
    409 = já contou hoje nesta etapa (não conte de novo)."""
    import secrets
    d = request.get_json(silent=True) or {}
    cod = _cod_do_texto(d.get("codigo"))
    etapa = str(d.get("etapa") or "").strip().lower()
    sku = str(d.get("sku") or "").strip()[:80] or None
    pedido = str(d.get("pedido") or "").strip()[:60] or None
    if not cod or etapa not in ("separacao", "expedicao", "full"):
        return jsonify({"success": False, "error": "Código da etiqueta ou etapa inválidos."}), 400
    garantir_tabelas()
    quem, agora, dia, tk = _quem(), _agora(), _dia_sp(), secrets.token_hex(6)
    with db_session() as c:
        e = c.execute("SELECT * FROM receb_etq WHERE codigo=?", (cod,)).fetchone()
        c.execute("INSERT INTO etq_uso (codigo, etapa, sku, pedido, quem, em, dia, tk) VALUES (?,?,?,?,?,?,?,?) "
                  "ON CONFLICT (codigo, etapa) DO NOTHING", (cod, etapa, sku, pedido, quem, agora, dia, tk))
        r = dict(c.execute("SELECT * FROM etq_uso WHERE codigo=? AND etapa=?", (cod, etapa)).fetchone())
        acao, antes = "usou", None
        if r["tk"] != tk:
            if r["dia"] == dia:
                _evento(c, cod, etapa, "repetida", sku, pedido, quem, f"já contada por {_txt_uso(r)}")
                return jsonify({"success": False, "tipo": "repetida", "codigo": cod, "antes": {k: r[k] for k in ("etapa", "pedido", "quem", "em")},
                                "antes_txt": _txt_uso(r),
                                "error": f"Essa unidade já foi bipada por {_txt_uso(r)}. Cada etiqueta conta uma vez: pegue outra unidade.",
                                "detalhe": "Se ela voltou para a prateleira, o supervisor libera no Rastrear."}), 409
            antes = r
            c.execute("UPDATE etq_uso SET sku=?, pedido=?, quem=?, em=?, dia=?, tk=? WHERE codigo=? AND etapa=? AND dia<>?",
                      (sku, pedido, quem, agora, dia, tk, cod, etapa, dia))
            r2 = c.execute("SELECT * FROM etq_uso WHERE codigo=? AND etapa=?", (cod, etapa)).fetchone()
            if not r2 or r2["tk"] != tk:            # outro aparelho bipou a mesma etiqueta no mesmo instante
                _evento(c, cod, etapa, "repetida", sku, pedido, quem, "bipada em dois aparelhos ao mesmo tempo")
                return jsonify({"success": False, "tipo": "repetida", "codigo": cod,
                                "error": "Essa unidade acabou de ser bipada em outro aparelho. Pegue outra unidade."}), 409
            acao = "de_novo"
        aviso = None
        if not e:
            acao, aviso = "desconhecida", "Etiqueta que o sistema não imprimiu (teste ou outra impressão): contou, mas ficou registrado."
        elif int(e["cancelado"] or 0):
            acao, aviso = "substituida", "Etiqueta trocada por outra impressão: contou, mas ficou registrado. Cole a etiqueta nova."
        det = f"saiu antes com {_txt_uso(antes)} ({antes['dia']})" if antes else None
        _evento(c, cod, etapa, acao, sku, pedido, quem, det)
    return jsonify({"success": True, "codigo": cod, "etapa": etapa, "aviso": aviso, "de_novo": bool(antes),
                    "unidade": ({"sku": e["sku"], "mult": int(e["mult"] or 1), "lote": e["lote"], "nota_id": e["nota_id"]} if e else None)})


@recebimento_routes.post("/recebimento/unidade/confirmar")
@login_required
def unidade_confirmar():
    """Depois que o bip contou: grava em qual pedido a unidade foi (a Separação só sabe depois que o servidor distribui)."""
    d = request.get_json(silent=True) or {}
    cod, etapa = _cod_do_texto(d.get("codigo")), str(d.get("etapa") or "").strip().lower()
    pedido = str(d.get("pedido") or "").strip()[:60] or None
    if not cod or etapa not in ETAPAS or not pedido:
        return jsonify({"success": False, "error": "Dados inválidos."}), 400
    garantir_tabelas()
    with db_session() as c:
        c.execute("UPDATE etq_uso SET pedido=? WHERE codigo=? AND etapa=?", (pedido, cod, etapa))
        ult = c.execute("SELECT id FROM etq_evento WHERE codigo=? AND etapa=? ORDER BY id DESC LIMIT 1", (cod, etapa)).fetchone()
        if ult:
            c.execute("UPDATE etq_evento SET pedido=? WHERE id=? AND pedido IS NULL", (pedido, ult["id"]))
    return jsonify({"success": True})


@recebimento_routes.post("/recebimento/unidade/soltar")
@login_required
def unidade_soltar():
    """Desfaz a reserva: o bip não contou (falhou/cancelou), o pedido foi limpo, ou o supervisor libera.
    Corpo: {etapa, codigo} ou {etapa, pedido} (todas as unidades daquele pedido nessa etapa). Operador só solta as dele."""
    d = request.get_json(silent=True) or {}
    etapa = str(d.get("etapa") or "").strip().lower()
    cod = _cod_do_texto(d.get("codigo")) if d.get("codigo") else None
    pedido = str(d.get("pedido") or "").strip()[:60] or None
    motivo = str(d.get("motivo") or "").strip()[:200] or None
    if etapa not in ("separacao", "expedicao", "full") or not (cod or pedido):
        return jsonify({"success": False, "error": "Dados inválidos."}), 400
    garantir_tabelas()
    quem, gestor = _quem(), eh_gestor()
    with db_session() as c:
        if cod:
            rows = c.execute("SELECT * FROM etq_uso WHERE codigo=? AND etapa=?", (cod, etapa)).fetchall()
        else:
            rows = c.execute("SELECT * FROM etq_uso WHERE pedido=? AND etapa=?", (pedido, etapa)).fetchall()
        soltas = []
        for r in rows:
            if r["quem"] != quem and not gestor:
                continue
            c.execute("DELETE FROM etq_uso WHERE codigo=? AND etapa=?", (r["codigo"], etapa))
            acao = "liberou" if (gestor and (d.get("liberar") or r["quem"] != quem)) else "soltou"
            _evento(c, r["codigo"], etapa, acao, r["sku"], r["pedido"], quem, motivo or f"era de {_txt_uso(dict(r))}")
            soltas.append(r["codigo"])
    if cod and not soltas:
        return jsonify({"success": False, "error": "Essa etiqueta não está contada nessa etapa (ou é de outra pessoa: só o supervisor libera)."}), 404
    return jsonify({"success": True, "soltas": soltas})


@recebimento_routes.get("/recebimento/unidade/<path:codigo>")
@login_required
def unidade_ver(codigo):
    """Tudo o que aconteceu com uma unidade: de qual nota veio, quem imprimiu, quem conferiu, quem separou/expediu e as tentativas recusadas."""
    cod = _cod_do_texto(codigo)
    if not cod:
        return jsonify({"success": False, "error": "Bipe ou digite a etiqueta do WMS (o código começa com U, ex.: U7K2M9QX)."}), 400
    garantir_tabelas()
    with db_session() as c:
        e = c.execute("SELECT * FROM receb_etq WHERE codigo=?", (cod,)).fetchone()
        etq = None
        if e:
            etq = dict(e)
            n = c.execute("SELECT numero, fornecedor_nome, status FROM receb_notas WHERE id=?", (e["nota_id"],)).fetchone()
            it = c.execute("SELECT descricao, sku, lote, validade FROM receb_itens WHERE id=?", (e["item_id"],)).fetchone()
            etq.update({"nota_numero": n["numero"] if n else None, "fornecedor": n["fornecedor_nome"] if n else None,
                        "nota_status": n["status"] if n else None, "descricao": it["descricao"] if it else None,
                        "validade": it["validade"] if it else None, "lote": e["lote"] or (it["lote"] if it else None)})
        usos = [dict(r) for r in c.execute("SELECT codigo, etapa, sku, pedido, quem, em, dia FROM etq_uso WHERE codigo=?", (cod,)).fetchall()]
        evs = [dict(r) for r in c.execute("SELECT id, etapa, acao, sku, pedido, quem, em, detalhe FROM etq_evento WHERE codigo=? "
                                         "ORDER BY em DESC, id DESC LIMIT 200", (cod,)).fetchall()]
    if not etq and not usos and not evs:
        return jsonify({"success": False, "error": f"{cod} não aparece em nada: a etiqueta não foi impressa por este sistema."}), 404
    ordem = list(ETAPAS)
    usos.sort(key=lambda u: ordem.index(u["etapa"]) if u["etapa"] in ordem else 99)
    return jsonify({"success": True, "codigo": cod, "etiqueta": etq, "usos": usos, "eventos": evs, "etapas": ETAPAS})


@recebimento_routes.get("/recebimento/unidade/ocorrencias")
@login_required
def unidade_ocorrencias():
    """Supervisor: bip repetido, etiqueta de fora / substituída / de outra nota e liberações, no período (padrão: últimos 7 dias)."""
    if not eh_gestor():
        return jsonify({"success": False, "error": "Só supervisor/admin."}), 403
    garantir_tabelas()
    def _ts(s, fim=False):
        try:
            t = datetime.datetime.strptime(str(s), "%Y-%m-%d") + datetime.timedelta(hours=3)   # meia-noite em Brasília
            return int((t - datetime.datetime(1970, 1, 1)).total_seconds()) + (86400 if fim else 0)
        except Exception:
            return None
    ate = _ts(request.args.get("ate"), True) or (_agora() + 60)
    de = _ts(request.args.get("de")) or (ate - 7 * 86400)
    marcas = ",".join("?" * len(OCORRENCIAS))
    with db_session() as c:
        evs = [dict(r) for r in c.execute(f"SELECT id, codigo, etapa, acao, sku, pedido, quem, em, detalhe FROM etq_evento "
                                          f"WHERE em>=? AND em<? AND acao IN ({marcas}) ORDER BY em DESC, id DESC LIMIT 1000",
                                          (de, ate) + OCORRENCIAS).fetchall()]
        tot = c.execute("SELECT COUNT(*) AS n FROM etq_evento WHERE em>=? AND em<? AND acao IN ('usou','de_novo','desconhecida','substituida')",
                        (de, ate)).fetchone()
    por = {}
    for ev in evs:
        p = por.setdefault(ev["quem"] or "?", {"quem": ev["quem"] or "?", "repetida": 0, "fora": 0, "liberou": 0})
        if ev["acao"] == "repetida":
            p["repetida"] += 1
        elif ev["acao"] == "liberou":
            p["liberou"] += 1
        else:
            p["fora"] += 1
    pessoas = sorted(por.values(), key=lambda p: (-p["repetida"], -p["fora"], p["quem"]))
    return jsonify({"success": True, "ocorrencias": evs, "pessoas": pessoas, "bipes": int(tot["n"] or 0) if tot else 0,
                    "de": _dia_sp(de), "ate": _dia_sp(ate - 1), "etapas": ETAPAS})


KITS_CACHE_S = 7 * 86400


def _kits_do_sku(sku, fresco=False):
    """Kits do Bling (produto com composição) feitos SÓ desse produto, ex.: KIT com 10 bolsas -> [{sku, nome, qtd}].
    Serve para a etiqueta: 700 bolsas vendidas em kit de 10 = 70 etiquetas do kit. Cache 7 dias."""
    key = str(sku or "").strip().lower()
    if not key:
        return []
    with db_session() as c:
        r = c.execute("SELECT dados, em FROM receb_kits WHERE sku_key=?", (key,)).fetchone()
    if r and not fresco and _agora() - int(r["em"] or 0) < KITS_CACHE_S:
        try:
            return json.loads(r["dados"] or "[]")
        except Exception:
            pass
    falhou = []

    def get(caminho, params=None):
        for k in range(4):                       # 429 = muitas chamadas seguidas: espera e tenta de novo
            st, txt, js = _bling_raw("GET", caminho, params=params)
            if st != 429:
                break
            time.sleep(1.5 + k)
        if st != 200:
            falhou.append(st)
        return st, txt, js

    st, txt, js = get("/produtos", {"codigos[]": sku, "limite": 5, "criterio": 5})
    if st != 200:
        return json.loads(r["dados"] or "[]") if r else []
    pid = next((int(x["id"]) for x in (js or {}).get("data") or [] if str(x.get("codigo") or "").strip().lower() == key), None)
    kits = []
    if pid:
        st, txt, js = get("/produtos", {"tipo": "E", "idComponente": pid, "criterio": 2, "limite": 30})
        for k in ((js or {}).get("data") or [])[:12] if st == 200 else []:
            st2, _, js2 = get(f"/produtos/estruturas/{k['id']}")
            comps = (((js2 or {}).get("data") or {}).get("componentes") or []) if st2 == 200 else []
            if len(comps) == 1 and int((comps[0].get("produto") or {}).get("id") or 0) == pid:
                q = float(comps[0].get("quantidade") or 0)
                if q >= 2 and k.get("codigo"):
                    kits.append({"sku": k["codigo"], "nome": k.get("nome") or k["codigo"], "qtd": q})
        kits.sort(key=lambda x: x["qtd"])
    if falhou:                                   # resposta incompleta do Bling: não guarda no cache (tenta de novo na próxima)
        return kits
    with db_session() as c:
        c.execute("DELETE FROM receb_kits WHERE sku_key=?", (key,))
        c.execute("INSERT INTO receb_kits (sku_key, dados, em) VALUES (?,?,?)", (key, json.dumps(kits, ensure_ascii=False), _agora()))
    return kits


@recebimento_routes.get("/recebimento/notas/<int:nid>/kits")
@login_required
def kits_da_nota(nid):
    """Para as etiquetas: por item, os kits do Bling feitos só daquele produto (ex.: kit de 10)."""
    garantir_tabelas()
    with db_session() as c:
        itens = [dict(x) for x in c.execute("SELECT id, sku FROM receb_itens WHERE nota_id=?", (nid,)).fetchall()]
    fresco = request.args.get("fresco") == "1"
    out, feitos = {}, {}
    for it in itens:
        sku = str(it.get("sku") or "").strip()
        if not sku:
            continue
        if sku.lower() not in feitos:
            try:
                feitos[sku.lower()] = _kits_do_sku(sku, fresco)
            except Exception as e:
                print(f"[RECEB] kits de {sku}: {e}", flush=True)
                feitos[sku.lower()] = []
        out[str(it["id"])] = feitos[sku.lower()]
    return jsonify({"kits": out})


@recebimento_routes.post("/recebimento/notas/<int:nid>/itens/<int:iid>/confirmar")
@login_required
def confirmar_item(nid, iid):
    """Botão "Está tudo certo": o item conferido é a quantidade da nota (ou a informada). Fica registrado quem confirmou."""
    d = request.get_json(silent=True) or {}
    garantir_tabelas()
    with db_session() as c:
        n = c.execute("SELECT status FROM receb_notas WHERE id=?", (nid,)).fetchone()
        it = c.execute("SELECT * FROM receb_itens WHERE id=? AND nota_id=?", (iid, nid)).fetchone()
        if not n or not it:
            return jsonify({"error": "Item não encontrado."}), 404
        if n["status"] in ("conferida", "lancada"):
            return jsonify({"error": "Nota já conferida — reabra para alterar."}), 409
        if _etq_ativa(c, nid) and not eh_gestor():
            return jsonify({"error": "Nesta nota a conferência é bipando a etiqueta de cada unidade. Só supervisor/admin ajusta na mão.",
                            "origem": "conferencia", "tipo": "precisa_etiqueta"}), 403
        try:
            q = float(d["qtd"]) if d.get("qtd") is not None else float(it["qtd_nf"] or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Quantidade inválida."}), 400
        sets, args = ["qtd_conferida=?", "ajuste_por=?"], [max(0.0, q), _quem()]
        for campo in ("lote", "validade"):
            if d.get(campo):
                sets.append(f"{campo}=?"); args.append(str(d[campo]).strip()[:60])
        _lote_serie_sets(d, sets, args)
        c.execute(f"UPDATE receb_itens SET {', '.join(sets)} WHERE id=?", tuple(args) + (iid,))
        c.execute("UPDATE receb_notas SET status='conferindo', atualizado_em=? WHERE id=? AND status IN ('aguardando','conferindo')", (_agora(), nid))
        novo = dict(c.execute("SELECT * FROM receb_itens WHERE id=?", (iid,)).fetchone())
    novo["qtd_nf"] = float(novo["qtd_nf"] or 0); novo["qtd_conferida"] = float(novo["qtd_conferida"] or 0)
    _num_bip(novo)
    return jsonify({"success": True, "item": novo, "falta": novo["qtd_nf"] - novo["qtd_conferida"]})


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
        for campo in ("lote", "validade", "obs", "fabricacao"):
            if campo in d:
                sets.append(f"{campo}=?"); args.append((str(d[campo]).strip() or None) if d[campo] is not None else None)
        _lote_serie_sets(d, sets, args)
        if "qtd_conferida" in d:
            try:
                nova = max(0.0, float(d["qtd_conferida"] or 0))
            except (TypeError, ValueError):
                nova = None
            if nova is not None and _etq_ativa(c, nid) and not eh_gestor():
                return jsonify({"error": "Nesta nota a conferência é bipando a etiqueta de cada unidade. Só supervisor/admin ajusta na mão.",
                                "origem": "conferencia", "tipo": "precisa_etiqueta"}), 403
            if nova is not None:
                atual, bip = float(it["qtd_conferida"] or 0), float(it["qtd_bipada"] or 0)
                if nova > atual and nova > bip and DIGITAR_SO_GESTOR and not eh_gestor():
                    return jsonify({"error": "Bipe cada unidade. Só supervisor/admin aumenta a quantidade na mão (produto sem código de barras).",
                                    "origem": "conferencia"}), 403
                sets.append("qtd_conferida=?"); args.append(nova)
                if nova < bip:
                    sets.append("qtd_bipada=?"); args.append(str(nova))
                if nova > bip:
                    sets.append("ajuste_por=?"); args.append(_quem())
        if sets:
            c.execute(f"UPDATE receb_itens SET {', '.join(sets)} WHERE id=?", tuple(args) + (iid,))
            c.execute("UPDATE receb_notas SET atualizado_em=? WHERE id=?", (_agora(), nid))
        novo = dict(c.execute("SELECT * FROM receb_itens WHERE id=?", (iid,)).fetchone())
    novo["qtd_nf"] = float(novo["qtd_nf"] or 0); novo["qtd_conferida"] = float(novo["qtd_conferida"] or 0)
    _num_bip(novo)
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
        if ETQ_UNICA == "fim":
            faltam = _faltam_etiquetas(c, nid)
            forcar = bool((request.get_json(silent=True) or {}).get("forcar")) and eh_gestor()
            if faltam and not forcar:
                det = "; ".join(f"item {f['n_item']} {str(f['descricao'] or '')[:30]}: "
                                + ("sem etiqueta impressa" if f["sem_etiqueta"] else f"bipou {f['bipadas']} de {f['conferido']}") for f in faltam[:4])
                return jsonify({"success": False, "tipo": "faltam_etiquetas", "faltam": faltam,
                                "error": f"Bipe todas as etiquetas para confirmar as unidades que entraram. {det}"}), 409
            if faltam:
                for f in faltam:
                    _evento(c, None, "recebimento", "liberou", None, d.get("numero"), _quem(),
                            f"fechou sem bipar todas: item {f['n_item']} bipou {f['bipadas']} de {f['conferido']}")
        motivos = _divergencias(d)
        agora = _agora()
        if motivos:
            c.execute("UPDATE receb_notas SET status='recusada', motivos=?, conferido_por=?, conferido_em=?, atualizado_em=? WHERE id=?",
                      ("\n".join(motivos), _quem(), agora, agora, nid))
            return jsonify({"success": True, "status": "recusada", "motivos": motivos})
        c.execute("UPDATE receb_notas SET status='conferida', motivos=NULL, conferido_por=?, conferido_em=?, atualizado_em=? WHERE id=?",
                  (_quem(), agora, agora, nid))
    entrada = ENTRADA_AUTO() and _disparar_entrada(nid, _quem())
    return jsonify({"success": True, "status": "conferida", "motivos": [], "entrada_auto": bool(entrada)})


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


# ═════════════════════════ ENTRADA NO BLING (depois de CONFERIDA) ═════════════════════════
# Decisão do Biel (12/11): o WMS lança tudo pela API: pedido de compra -> Atendido, estoque no Local, lotes
# (só produto com controle de lote ligado) e contas a pagar das duplicatas do XML. O XML entra no Bling depois
# só como registro fiscal (importar SEM lançar estoque/contas). A API do Bling não importa XML de entrada.
# Roda em segundo plano, passo a passo; cada passo fica gravado (entrada_resumo) e no histórico (receb_entrada_log),
# então se parar no meio, rodar de novo continua de onde parou e nunca lança 2 vezes.
ENTRADA_AUTO = lambda: os.environ.get("RECEB_ENTRADA_AUTO", "0") == "1"     # 1 = já dispara ao conferir
DEP_LOCAL_ID = int(os.environ.get("BLING_DEPOSITO_LOCAL_ID", "10488672491"))
ETAPAS_ENT = {"preparando": "Conferindo pedido, produtos e lotes no Bling", "nota": "Procurando a nota no Bling",
              "robo": "Robô manifestando e importando a nota no Bling",
              "aguardando_nota": "Esperando a nota ser manifestada e importada no Bling",
              "vinculo": "Robô conciliando a nota com o pedido de compra no Bling", "pedido": "Mudando o pedido de compra para Atendido",
              "estoque": "Lançando o estoque no Local", "lotes": "Lançando os lotes", "contas": "Lançando as contas a pagar",
              "ok": "Entrada concluída", "erro": "Parou com erro", "desfazendo": "Desfazendo a entrada"}
_ent_ativos: set = set()
_ent_lock = threading.Lock()
_sit_atendido_cache = {}


def _bling_raw(metodo, caminho, payload=None, params=None):
    """Chamada ao Bling. Só repete no 429 ("limite de requisições por segundo"): o Bling recusa ANTES de processar,
    então repetir não lança 2 vezes. Erro de rede/timeout NÃO repete. -> (status, texto, json|None)"""
    import requests
    b = _bling()
    if not b:
        return 0, "Bling indisponível", None
    for tentativa in range(4):
        _ritmo()
        try:
            r = requests.request(metodo, f"{b.base_url}{caminho}", headers=b._headers(), json=payload, params=params, timeout=(10, 60))
        except Exception as e:
            return 0, f"sem resposta do Bling: {e}", None
        if r.status_code != 429 or tentativa == 3:
            break
        time.sleep(1.2 * (tentativa + 1))
    try:
        js = r.json()
    except Exception:
        js = None
    return r.status_code, (r.text or "")[:2000], js


def _msg_bl(txt):
    try:
        e = json.loads(txt).get("error") or {}
        partes = [e.get("description") or e.get("message") or ""]
        for f in e.get("fields") or []:
            partes.append(f"{f.get('element') or ''}: {f.get('msg') or ''}".strip(": "))
        return " | ".join(p for p in partes if p)[:500]
    except Exception:
        return str(txt or "")[:500]


def _ent_log(nid, acao, ok, http=None, mensagem="", resposta="", enviado=None, por=None):
    try:
        with db_session() as c:
            c.execute("INSERT INTO receb_entrada_log (nota_id, acao, ok, http, mensagem, resposta, enviado, por, em) VALUES (?,?,?,?,?,?,?,?,?)",
                      (nid, acao, 1 if ok else 0, http, str(mensagem or "")[:1000], str(resposta or "")[:4000],
                       json.dumps(enviado, ensure_ascii=False)[:6000] if enviado is not None else None, por, _agora()))
    except Exception as e:
        print(f"[ENTRADA] log falhou ({nid}): {e}", flush=True)


def _ent_estado(nid, etapa, **campos):
    sets, vals = ["entrada_etapa=?", "entrada_em=?"], [etapa, str(_agora())]
    for k, v in campos.items():
        sets.append(f"{k}=?")
        vals.append(v if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False))
    with db_session() as c:
        c.execute(f"UPDATE receb_notas SET {', '.join(sets)} WHERE id=?", (*vals, nid))


def _ent_resumo(r):
    try:
        return json.loads(r["entrada_resumo"] or "{}")
    except Exception:
        return {}


def _ent_rodando(r):
    try:
        lim = 900 if r["entrada_etapa"] in ("robo", "vinculo") else 300
        return r["entrada_etapa"] in ("preparando", "nota", "robo", "vinculo", "pedido", "estoque", "lotes", "contas", "desfazendo") and \
            _agora() - int(float(r["entrada_em"] or 0)) < lim
    except Exception:
        return False


def _situacao_pc(nome_alvo="atendido"):
    """id da situação (da conta) do módulo de pedidos de compra. Ex.: 'atendido'. Env BLING_SITUACAO_PC_ATENDIDO_ID manda."""
    if nome_alvo == "atendido" and os.environ.get("BLING_SITUACAO_PC_ATENDIDO_ID"):
        return int(os.environ["BLING_SITUACAO_PC_ATENDIDO_ID"])
    if nome_alvo in _sit_atendido_cache:
        return _sit_atendido_cache[nome_alvo]
    import unicodedata
    nz = lambda t: unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower().strip()
    st, _, js = _bling_raw("GET", "/situacoes/modulos")
    for m in ((js or {}).get("data") or []):
        if "compra" in nz(m.get("nome")) or "compra" in nz(m.get("descricao")):
            st2, _, js2 = _bling_raw("GET", f"/situacoes/modulos/{m['id']}")
            for s0 in ((js2 or {}).get("data") or []):
                if nz(s0.get("nome")) == nome_alvo:
                    _sit_atendido_cache[nome_alvo] = int(s0["id"])
                    return int(s0["id"])
    return None


def _saldos_local(ids):
    """{id_produto: saldo físico no depósito Local}"""
    out = {}
    ids = [int(i) for i in ids if i]
    for k in range(0, len(ids), 20):
        st, txt, js = _bling_raw("GET", "/estoques/saldos", params=[("idsProdutos[]", i) for i in ids[k:k + 20]])
        for d in ((js or {}).get("data") or []):
            pid = int((d.get("produto") or {}).get("id") or 0)
            for dep in d.get("depositos") or []:
                if int(dep.get("id") or 0) == DEP_LOCAL_ID:
                    out[pid] = float(dep.get("saldoFisico") or 0)
            out.setdefault(pid, 0.0)
    return out


def _dups_xml(xml):
    out = []
    for d in re.findall(r"<dup>(.*?)</dup>", xml or "", re.S):
        g = lambda t: (re.search(rf"<{t}>(.*?)</{t}>", d) or [None, ""])[1]
        try:
            out.append({"numero": g("nDup"), "vencimento": g("dVenc"), "valor": round(float(g("vDup") or 0), 2)})
        except ValueError:
            pass
    return out


_TPAG_NOME = {1: "dinheiro", 2: "cheque", 3: "cartão de crédito", 4: "cartão de débito", 15: "boleto", 16: "depósito", 17: "pix",
              18: "transferência"}


def _pag_xml(xml):
    """Meio de pagamento da NF-e: (tPag, texto). <detPag><tPag> (e <xPag> no 99); 90 (sem pagamento) / sem detPag com
    duplicata (<cobr><dup>) = boleto (15)."""
    pags = re.findall(r"<detPag>(.*?)</detPag>", xml or "", re.S)
    lst = []
    for d in pags:
        t = (re.search(r"<tPag>\s*(\d+)\s*</tPag>", d) or [None, ""])[1]
        x = (re.search(r"<xPag>(.*?)</xPag>", d, re.S) or [None, ""])[1].strip()
        if t.isdigit():
            lst.append((int(t), x))
    for t, x in lst:
        if t not in (90, 99):
            return t, _TPAG_NOME.get(t, f"tPag {t}")
    for t, x in lst:
        if t == 99 and x:
            return 99, x
    if re.search(r"<dup>", xml or ""):
        return 15, "boleto (duplicatas da nota)"
    return (lst[0][0] if lst else None), ""


def _formas_bling():
    st, _, js = _bling_raw("GET", "/formas-pagamentos", params={"limite": 100})
    return [x for x in ((js or {}).get("data") or []) if str(x.get("situacao", 1)) in ("1", "True", "true")] if st == 200 else []


def _uso_formas():
    geral = {}
    for pag in (1, 2, 3):
        st, _, js = _bling_raw("GET", "/contas/pagar", params={"pagina": pag, "limite": 100})
        lst = (js or {}).get("data") or []
        for x in lst:
            f = (x.get("formaPagamento") or {}).get("id")
            if f:
                geral[int(f)] = geral.get(int(f), 0) + 1
        if len(lst) < 100:
            break
    return geral


def _forma_pagamento(id_fornecedor, xml=None, formas_pc=None):
    """Forma de pagamento das contas. 1º a forma das parcelas do PEDIDO DE COMPRA (é a combinada com o fornecedor; NF 70644
    DORJA 23/11: a NF-e dizia tPag 14 duplicata e o Bling não tem forma desse tipo, mas o pedido tinha BOLETO 28/56/84DDL).
    2º a da NF-e (Biel 18/11): a forma ativa do Bling com o mesmo tPag (pelo tipoPagamento ou pelo nome; no 99, pelo <xPag>);
    empate = a mais usada. 3º RECEB_FORMA_PAGAMENTO_ID do .env; senão não lança e diz o que a nota pede.
    -> (id, de onde veio) ou (None, explicação)."""
    tp, txt = _pag_xml(xml)
    formas = _formas_bling()
    ids_pc = [int(x) for x in (formas_pc or [])]
    if ids_pc:
        for x in formas:
            if int(x.get("id") or 0) in ids_pc:
                return int(x["id"]), f"{x.get('descricao')} (pelo pedido de compra)"
        return ids_pc[0], f"id {ids_pc[0]} (pelo pedido de compra)"
    cands = [x for x in formas if tp and str(x.get("tipoPagamento") or "").lstrip("0") == str(tp)] if tp != 99 else []
    if not cands and txt:
        chave = re.sub(r"\(.*?\)", "", txt).strip().lower().split()[0] if txt.strip() else ""
        cands = [x for x in formas if chave and chave[:5] in str(x.get("descricao") or "").lower()]
    if cands:
        if len(cands) > 1:
            uso = _uso_formas()
            cands.sort(key=lambda x: (-uso.get(int(x["id"]), 0), 0 if x.get("padrao") else 1))
        f = cands[0]
        return int(f["id"]), f"{f.get('descricao')} (pela NF-e: {txt or 'tPag ' + str(tp)})"
    if os.environ.get("RECEB_FORMA_PAGAMENTO_ID"):
        return int(os.environ["RECEB_FORMA_PAGAMENTO_ID"]), f"configurada no WMS (a NF-e diz {txt or tp or 'nada'}, sem forma igual no Bling)"
    return None, (f"a NF-e diz {txt or 'tPag ' + str(tp)} e não achei forma assim no Bling" if tp else "a NF-e não diz o meio de pagamento") + \
        (". Formas no Bling: " + ", ".join(f"{x.get('descricao')} (id {x.get('id')})" for x in formas[:8]) if formas else "")


VIGIA_S = int(os.environ.get("RECEB_VIGIA_S", "180"))          # de quanto em quanto tempo procura a nota no Bling
ROBO_ENTRADA = lambda: os.environ.get("RECEB_ROBO_ENTRADA", "1") == "1"   # robô manifesta (Confirmo) e importa a nota
_vigia = {"on": False}


def _nf_bling(chave):
    """Nota do fornecedor já manifestada e importada no Bling (pela chave). -> (detalhe | None, erro | None)"""
    chave = _dig(chave)
    if len(chave) != 44:
        return None, "chave inválida"
    for params in ({"chaveAcesso": chave, "limite": 5}, {"tipo": 0, "chaveAcesso": chave, "limite": 5}):
        st, txt, js = _bling_raw("GET", "/nfe", params=params)
        if st == 429:
            time.sleep(2)
            st, txt, js = _bling_raw("GET", "/nfe", params=params)
        if st != 200:
            return None, _msg_bl(txt) or f"HTTP {st}"
        for n in (js or {}).get("data") or []:
            if _dig(n.get("chaveAcesso")) == chave and int(n.get("situacao") or 0) != 2:
                st2, txt2, js2 = _bling_raw("GET", f"/nfe/{n['id']}")
                det = (js2 or {}).get("data") or {}
                return (det or n), None
    return None, None


def _ja_lancado(txt):
    """O Bling recusou porque JÁ estava lançado (ex.: lançaram ao importar a nota)."""
    t = _txt_norm(_msg_bl(txt) or txt or "")
    return bool(re.search(r"ja (foi |esta |estao |foram )?(lancad|estornad)|ja possui (lancament|conta)|already", t))


def _conciliar_pc(nid, r, pc, nf, quem):
    """Concilia a nota com o pedido de compra NO BLING, pelo robô (tela "Conciliação dos itens").
    Pela API não dá: PUT /pedidos/compras com itens[].notaFiscal volta 200 e o Bling não grava (testado 12/11/2026).
    E o Bling TRAVA a conciliação depois que a nota lança estoque ou contas: por isso é antes do estoque."""
    if not ROBO_ENTRADA():
        return {"ok": False, "pulado": True, "msg": "robô desligado (RECEB_ROBO_ENTRADA=0): a nota fica sem conciliação com o pedido"}
    try:
        from services.bling_entrada_robo import conciliar_pedido
        # SKU de cada item da nota, na ordem da NF: item que o Bling marcou como "Novo produto" (sem vínculo com o cadastro)
        # o robô liga ao produto certo antes de conciliar o pedido (NF 13003, Dermafreeze Mini, 19/11)
        skus = []
        try:
            with db_session() as c:
                skus = [x["sku"] for x in c.execute("SELECT sku FROM receb_itens WHERE nota_id=? ORDER BY n_item, id", (nid,)).fetchall()]
        except Exception:
            skus = []
        out = conciliar_pedido(r["numero"], pc["numero"], valor=float(r["valor"] or 0) or None,
                               n_itens=len(nf.get("itens") or []) or None, skus=skus, log=lambda t: print(t, flush=True))
    except Exception as e:
        msg = str(e)
        trav = "TRAVADA" in msg
        msg = msg.replace("TRAVADA: ", "")
        _ent_log(nid, f"conciliar a nota com o pedido {pc['numero']} (robô)", False, None, msg[:500], por=quem)
        return {"ok": False, "pulado": trav, "msg": msg[:300]}
    msg = f"{out.get('itens')} item(ns) conciliados com o pedido {pc['numero']} (robô, na tela do Bling)"
    _ent_log(nid, f"conciliar a nota com o pedido {pc['numero']} (robô)", True, None, msg + " · " + " · ".join(out.get("passos") or [])[:400], por=quem)
    return {"ok": True, "robo": True, "msg": msg, "itens": out.get("itens")}


def _contas_existentes(forn, contas):
    """Contas a pagar do fornecedor com o mesmo vencimento e valor das duplicatas (ex.: lançadas ao importar a nota)."""
    if not forn or not contas:
        return {}
    vs = sorted(str(c["vencimento"])[:10] for c in contas if c.get("vencimento"))
    if not vs:
        return {}
    st, txt, js = _bling_raw("GET", "/contas/pagar", params={"idContato": forn, "dataVencimentoInicial": vs[0],
                                                            "dataVencimentoFinal": vs[-1], "limite": 100})
    lst = (js or {}).get("data") or []
    out, usados = {}, set()
    for k, ct in enumerate(contas):
        for x in lst:
            if x.get("id") in usados or int(x.get("situacao") or 1) in (4, 5):     # 4/5: cancelada/excluída
                continue
            if str(x.get("vencimento") or "")[:10] == str(ct["vencimento"])[:10] and abs(float(x.get("valor") or 0) - ct["valor"]) < 0.01:
                usados.add(x.get("id"))
                out[str(ct["numero"] or k + 1)] = {"id": x.get("id"), "valor": ct["valor"], "vencimento": ct["vencimento"], "via": "existente"}
                break
    return out


def _vigia_ligar():
    """Liga (uma vez por processo) quem procura no Bling as notas que estão esperando ser importadas."""
    with _ent_lock:
        if _vigia["on"]:
            return
        _vigia["on"] = True
    threading.Thread(target=_vigia_loop, daemon=True).start()


def _vigia_loop():
    while True:
        time.sleep(VIGIA_S)
        try:
            with db_session() as c:
                rows = [dict(x) for x in c.execute("SELECT id, chave, entrada_por FROM receb_notas WHERE entrada_etapa='aguardando_nota'").fetchall()]
            if not rows:
                with _ent_lock:
                    _vigia["on"] = False
                return
            for r in rows:
                nf, _ = _nf_bling(r["chave"])
                if not nf:
                    continue
                with db_session() as c:       # pega para si (outro processo não dispara junto)
                    pegou = c.execute("UPDATE receb_notas SET entrada_etapa='preparando', entrada_em=? WHERE id=? AND entrada_etapa='aguardando_nota' RETURNING id",
                                      (str(_agora()), r["id"])).fetchone()
                if pegou:
                    print(f"[ENTRADA] nota {r['id']} apareceu no Bling: continuando a entrada", flush=True)
                    _disparar_entrada(r["id"], f"{r['entrada_por'] or 'automático'} (automático)")
        except Exception as e:
            print(f"[ENTRADA] vigia: {e}", flush=True)


def _rastros_xml(xml_txt):
    """{n_item: [{lote, fab, val}]} dos <rastro> do XML (dFab = data de fabricação, que o Bling exige para criar o lote)."""
    out = {}
    try:
        root = ET.fromstring(xml_txt.encode("utf-8") if isinstance(xml_txt, str) else xml_txt)
    except Exception:
        return out
    for det in root.iter(f"{{{NS['n']}}}det"):
        n = int(det.get("nItem") or 0)
        for ras in det.iter(f"{{{NS['n']}}}rastro"):
            g = lambda t: (ras.findtext(f"n:{t}", default="", namespaces=NS) or "").strip()
            out.setdefault(n, []).append({"lote": g("nLote"), "fab": g("dFab")[:10], "val": g("dVal")[:10]})
    return out


def _data_ok(iso):
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except Exception:
        return None
    hoje = datetime.date.today()
    return d.isoformat() if hoje - datetime.timedelta(days=365 * 15) <= d <= hoje else None


def _fabricacao_auto(lote, rastros):
    """Data de fabricação sem perguntar: 1) <rastro><dFab> do XML desse lote; 2) nº do lote que é uma data (AAAAMMDD, ex.: 20250715)."""
    lote = str(lote or "").strip().upper()
    rs = rastros or []
    for r in rs:
        if r.get("fab") and str(r.get("lote") or "").strip().upper() == lote and _data_ok(r["fab"]):
            return _data_ok(r["fab"]), "xml"
    if len(rs) == 1 and rs[0].get("fab") and _data_ok(rs[0]["fab"]) and not rs[0].get("lote"):
        return _data_ok(rs[0]["fab"]), "xml"
    m = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", lote)
    if m and _data_ok(f"{m.group(1)}-{m.group(2)}-{m.group(3)}"):
        return _data_ok(f"{m.group(1)}-{m.group(2)}-{m.group(3)}"), "lote"
    return None, None


def plano_entrada(nid, resumo=None):
    """Monta TUDO sem gravar nada. -> dict com pontos (ok/erro/aviso), bloqueio e o que vai ser feito."""
    pontos = []
    plano = {"pontos": pontos, "bloqueio": None, "itens": [], "contas": [], "pc": None}

    def ponto(ok, item, det=""):
        pontos.append({"ok": ok, "item": item, "detalhe": det})
        if ok is False and not plano["bloqueio"]:
            plano["bloqueio"] = f"{item}: {det}"

    with db_session() as c:
        r = c.execute("SELECT * FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not r:
            ponto(False, "Nota", "não encontrada")
            return plano
        itens = [dict(x) for x in c.execute("SELECT * FROM receb_itens WHERE nota_id=? ORDER BY n_item", (nid,)).fetchall()]
    plano["nota"] = {"numero": r["numero"], "serie": r["serie"], "fornecedor": r["fornecedor_nome"]}
    ponto(r["status"] == "conferida", "Conferência", "nota conferida" if r["status"] == "conferida" else f"a nota está '{r['status']}': confira antes")
    if not r["pedido_compra_id"]:
        ponto(False, "Pedido de compra", "nenhum pedido vinculado")
        return plano
    st, txt, js = _bling_raw("GET", f"/pedidos/compras/{r['pedido_compra_id']}")
    pc = (js or {}).get("data") or {}
    if st != 200 or not pc:
        ponto(False, "Pedido de compra", f"não consegui ler o pedido {r['pedido_compra_numero']} no Bling ({_msg_bl(txt) or st})")
        return plano
    sit = int((pc.get("situacao") or {}).get("valor") or 0)
    plano["pc"] = {"id": pc["id"], "numero": pc.get("numero"), "situacao_id": (pc.get("situacao") or {}).get("id"), "situacao_valor": sit,
                   "fornecedor_id": (pc.get("fornecedor") or {}).get("id"), "categoria_id": (pc.get("categoria") or {}).get("id"),
                   # forma de pagamento combinada no pedido (parcelas): vale antes da que vem no XML (NF 70644 DORJA, 23/11)
                   "formas_pagamento": [int(x) for x in {str((pa.get("formaPagamento") or {}).get("id") or "") for pa in (pc.get("parcelas") or [])} if x.isdigit()]}
    nome_sit = {0: "Em aberto", 1: "Atendido", 2: "Cancelado", 3: "Em andamento"}.get(sit, str(sit))
    if sit == 2:
        ponto(False, "Pedido de compra", f"o pedido {pc.get('numero')} está CANCELADO no Bling")
    elif sit == 1 and ((resumo or {}).get("pedido") or ((resumo or {}).get("vinculo") or {}).get("robo")):
        ponto(True, "Pedido de compra", f"pedido {pc.get('numero')} já Atendido (feito pelo WMS nesta entrada)")
    elif sit == 1:
        ponto(False, "Pedido de compra", f"o pedido {pc.get('numero')} já está Atendido no Bling: o estoque pode já ter entrado. "
                                         "Confira; se não entrou, volte o pedido para Em andamento e rode de novo")
    else:
        sid = _situacao_pc("atendido")
        plano["pc"]["atendido_id"] = sid
        ponto(True if sid else False, "Pedido de compra", f"pedido {pc.get('numero')} ({nome_sit}) vai para Atendido" if sid
              else "não achei a situação 'Atendido' dos pedidos de compra no Bling")

    # a nota do fornecedor no Bling (manifestada e importada): é dela que sai o estoque e as contas
    nf, nf_err = ((resumo or {}).get("nota"), None) if (resumo or {}).get("nota") else _nf_bling(r["chave"])
    plano["nf"] = nf
    if nf:
        ponto(True, "Nota no Bling", f"NF {r['numero']} está no Bling (id {nf.get('id')}): o WMS vincula ao pedido e lança estoque e contas DA NOTA")
    elif nf_err:
        ponto(None, "Nota no Bling", f"não consegui procurar a nota no Bling ({nf_err})")
    else:
        ponto(None, "Nota no Bling", f"a NF {r['numero']} ainda não está no Bling: manifeste e importe SEM lançar estoque nem contas. "
                                     "O WMS continua sozinho quando ela aparecer")

    # itens: produto do Bling pelo item do pedido (SKU / código do fornecedor)
    pc_itens = [{"sku": (i.get("produto") or {}).get("codigo"), "cprod": i.get("codigoFornecedor"), "pid": (i.get("produto") or {}).get("id"),
                 "valor": i.get("valor"), "qtd": i.get("quantidade")} for i in pc.get("itens") or []]
    ids = []
    rastros = None
    for it in itens:
        x = _item_pc({"itens": pc_itens}, it) if it.get("sku") or it.get("cprod") else None
        pid = (x or {}).get("pid")
        q = float(it["qtd_conferida"] or 0)
        lote_it = (it.get("lote") or "").strip()
        fab, fab_origem = ((it.get("fabricacao") or "").strip()[:10], "informada") if (it.get("fabricacao") or "").strip() else (None, None)
        if not fab and lote_it:
            if rastros is None:
                rastros = _rastros_xml(r["xml"] or "")
            fab, fab_origem = _fabricacao_auto(lote_it, rastros.get(int(it["n_item"] or 0)))
        plano["itens"].append({"item_id": it["id"], "n_item": it["n_item"], "sku": it["sku"], "descricao": it["descricao"], "pid": pid,
                               "qtd": q, "custo": float(it.get("valor_unit") or 0), "lote": lote_it,
                               "validade": (it.get("validade") or "").strip(), "validade_indet": _tem_validade(it) and not (it.get("validade") or "").strip(),
                               "serie": (it.get("serie") or "").strip(), "controla_lote": False,
                               "fabricacao": fab or "", "fabricacao_origem": fab_origem, "lote_existe": False})
        if not pid:
            ponto(False, f"Item {it['n_item']}", f"{(it['descricao'] or '')[:40]}: não achei o produto no pedido (SKU {it['sku'] or '-'})")
        else:
            ids.append(pid)
    if ids:
        st, txt, js = _bling_raw("GET", "/produtos/lotes/controla-lote", params=[("idsProdutos[]", i) for i in ids])
        ctl = {int(x.get("idProduto")): bool(x.get("controlaLote")) for x in ((js or {}).get("data") or [])}
        for p in plano["itens"]:
            if p["pid"]:
                p["controla_lote"] = ctl.get(int(p["pid"]), False)
                if p["controla_lote"] and not (p["lote"] and (p["validade"] or p["validade_indet"])):
                    ponto(False, f"Item {p['n_item']}", f"{(p['descricao'] or '')[:40]}: o Bling controla lote desse produto e falta lote/validade")
                elif p["controla_lote"] and not p["fabricacao"]:
                    # o Bling só pede fabricação para CRIAR o lote: se o lote já existe nesse produto, não precisa
                    st2, _, js2 = _bling_raw("GET", "/produtos/lotes", params={"idsProdutos[]": p["pid"]})
                    p["lote_existe"] = any(str(l.get("codigoLote") or "").strip().upper() == p["lote"].upper() for l in ((js2 or {}).get("data") or []))
                    if not p["lote_existe"]:
                        ponto(False, f"Item {p['n_item']}", f"{(p['descricao'] or '')[:40]}: falta a DATA DE FABRICAÇÃO do lote {p['lote']} "
                                                          "(o Bling exige para criar o lote): informe aqui embaixo")
    n_lote = sum(1 for p in plano["itens"] if p["controla_lote"])
    ponto(True, "Estoque", f"{sum(p['qtd'] for p in plano['itens']):g} unidades em {len(plano['itens'])} produtos no depósito Local"
          + (f" · {n_lote} com lote" if n_lote else ""))

    # contas a pagar = duplicatas do XML
    dups = _dups_xml(r["xml"])
    forn = plano["pc"]["fornecedor_id"]
    if not dups:
        ponto(None, "Contas a pagar", "o XML não tem duplicatas: nenhuma conta será lançada")
    else:
        fp, origem_fp = _forma_pagamento(forn, r["xml"], plano["pc"].get("formas_pagamento"))
        if not fp:
            ponto(False, "Contas a pagar", f"forma de pagamento: {origem_fp}. Cadastre essa forma no Bling (ou ponha RECEB_FORMA_PAGAMENTO_ID=<id> no .env)")
        tot = round(sum(d["valor"] for d in dups), 2)
        for k, d in enumerate(dups):
            plano["contas"].append({"numero": d["numero"], "vencimento": d["vencimento"], "valor": d["valor"],
                                    "payload": {k2: v2 for k2, v2 in {"vencimento": d["vencimento"], "valor": d["valor"], "contato": {"id": forn},
                                                "formaPagamento": {"id": fp}, "dataEmissao": str(r["emissao"] or "")[:10] or None,
                                                "numeroDocumento": f"{r['numero']}/{d['numero'] or k + 1}",
                                                "historico": f"NF {r['numero']} - {str(r['fornecedor_nome'] or '')[:40]} - parcela {k + 1}/{len(dups)} (pedido {pc.get('numero')}, WMS)",
                                                **({"categoria": {"id": plano['pc']['categoria_id']}} if plano['pc']['categoria_id'] else {})}.items() if v2 is not None}})
        if fp:
            ponto(True, "Contas a pagar", f"{len(dups)} parcela(s), total {_brl(tot)} · forma de pagamento {fp} ({origem_fp})")
    return plano


def _job_entrada(nid, quem):
    """Executa a entrada. Passos já feitos (entrada_resumo) são pulados: rodar de novo continua de onde parou."""
    try:
        _ent_estado(nid, "preparando", entrada_erro=None, entrada_por=quem)
        with db_session() as c:
            r0 = c.execute("SELECT entrada_resumo FROM receb_notas WHERE id=?", (nid,)).fetchone()
        p = plano_entrada(nid, _ent_resumo(r0) if r0 else None)
        if p["bloqueio"]:
            _ent_log(nid, "conferir", False, None, p["bloqueio"], por=quem)
            _ent_estado(nid, "erro", entrada_erro=json.dumps({"origem": "conferencia", "error": p["bloqueio"], "pontos": p["pontos"]}, ensure_ascii=False),
                        entrada_msg=p["bloqueio"][:500])
            return
        with db_session() as c:
            r = c.execute("SELECT * FROM receb_notas WHERE id=?", (nid,)).fetchone()
        res = _ent_resumo(r)
        salvar = lambda: _ent_estado(nid, res.get("_etapa", "preparando"), entrada_resumo=res)
        pc = p["pc"]
        ids = [int(x["pid"]) for x in p["itens"] if x["pid"]]

        # foto do estoque ANTES de qualquer coisa (antes do robô/alguém importar): se o Bling lançar o estoque ao importar,
        # o WMS percebe pelo salto e não lança de novo
        if "saldo_inicio" not in res and ids:
            res["saldo_inicio"] = {str(k): v for k, v in _saldos_local(ids).items()}
            salvar()

        # 0) a nota do fornecedor no Bling. A API não manifesta nem importa: se não estiver lá, o ROBÔ faz isso
        #    (Confirmo a operação + importar). Se o robô não conseguir, fica esperando alguém fazer e continua sozinho.
        if not res.get("nota"):
            res["_etapa"] = "nota"; _ent_estado(nid, "nota")
            nf, err = _nf_bling(r["chave"])
            if not nf and not err and ROBO_ENTRADA():
                res["_etapa"] = "robo"; _ent_estado(nid, "robo")
                try:
                    from services.bling_entrada_robo import manifestar_e_importar
                    rb = manifestar_e_importar(r["numero"], r["chave"], float(r["valor"] or 0) or None)
                    _ent_log(nid, "robô: manifestar e importar", True, None, " · ".join(rb.get("passos") or []), por=quem)
                    for _ in range(12):                # a API pode levar alguns segundos para enxergar
                        nf, err = _nf_bling(r["chave"])
                        if nf:
                            break
                        time.sleep(5)
                except Exception as e:
                    _ent_log(nid, "robô: manifestar e importar", False, None, str(e)[:900], por=quem)
                    res["robo_erro"] = str(e)[:400]
            if not nf:
                if err:
                    raise RuntimeError(f"BLING|Não consegui procurar a nota no Bling: {err}")
                msg = (f"Esperando a NF {r['numero']} no Bling" + (f" (o robô não conseguiu: {res['robo_erro'][:160]})" if res.get("robo_erro") else "")
                       + ". Manifeste (Confirmo a operação) e importe a nota no Bling, SEM lançar estoque nem contas. "
                       f"O WMS confere a cada {max(1, VIGIA_S // 60)} min e continua sozinho.")
                _ent_log(nid, "nota no Bling", True, None, msg, por=quem)
                _ent_estado(nid, "aguardando_nota", entrada_resumo=res, entrada_msg=msg[:500])
                _vigia_ligar()
                return
            if int(nf.get("tipo") if nf.get("tipo") is not None else 0) != 0:
                raise RuntimeError(f"CONF|A nota achada no Bling com essa chave não é de entrada (id {nf.get('id')})")
            res.pop("robo_erro", None)
            res["nota"] = {"id": nf.get("id"), "numero": nf.get("numero"), "situacao": nf.get("situacao"),
                           "parcelas": len(nf.get("parcelas") or []),
                           "itens": [{"codigo": it.get("codigo"), "qtd": float(it.get("quantidade") or 0)} for it in nf.get("itens") or []]}
            _ent_log(nid, "nota no Bling", True, None, f"NF {r['numero']} está no Bling (id {nf.get('id')}, "
                     f"{len(nf.get('itens') or [])} itens, {len(nf.get('parcelas') or [])} parcelas)", por=quem)
            salvar()
        nf_id = res["nota"]["id"]

        # 0b) conciliar no Bling: a nota vinculada ao pedido de compra (não trava a entrada se o Bling recusar)
        vin0 = res.get("vinculo") or {}
        if not (vin0.get("ok") and vin0.get("robo")) and not vin0.get("pulado"):
            res["_etapa"] = "vinculo"; _ent_estado(nid, "vinculo")
            if res.get("estoque") or res.get("contas") or res.get("contas_via"):
                res["vinculo"] = {"ok": False, "pulado": True,
                                  "msg": "o estoque/contas dessa nota já entraram e o Bling trava a conciliação depois disso: fica sem vínculo com o pedido"}
            else:
                res["vinculo"] = _conciliar_pc(nid, r, pc, res["nota"], quem)
            salvar()
            if not res["vinculo"]["ok"] and not res["vinculo"].get("pulado"):
                raise RuntimeError(f"VINC|O robô não conseguiu conciliar a nota com o pedido {pc['numero']} no Bling: {res['vinculo']['msg']}. "
                                   "Clique Continuar para tentar de novo, ou Seguir sem conciliar (o Bling trava a conciliação depois do estoque).")

        # 1) pedido -> Atendido (medindo o estoque antes/depois: o Bling pode lançar sozinho ao atender)
        if not res.get("pedido"):
            res["_etapa"] = "pedido"; _ent_estado(nid, "pedido")
            antes = _saldos_local(ids)
            st0, _, js0 = _bling_raw("GET", f"/pedidos/compras/{pc['id']}")
            if st0 == 200:
                pc["situacao_valor"] = int((((js0 or {}).get("data") or {}).get("situacao") or {}).get("valor") or pc["situacao_valor"])
            if pc["situacao_valor"] != 1:
                st, txt, _ = _bling_raw("PATCH", f"/pedidos/compras/{pc['id']}/situacoes/{pc['atendido_id']}")
                ok = st in (200, 204)
                _ent_log(nid, f"pedido {pc['numero']} -> Atendido", ok, st, "situação alterada" if ok else _msg_bl(txt), txt, por=quem)
                if not ok:
                    raise RuntimeError(f"BLING|O Bling recusou mudar o pedido para Atendido: {_msg_bl(txt) or st}")
            depois = _saldos_local(ids)
            res["pedido"] = {"anterior_id": pc["situacao_id"], "antes": {str(k): v for k, v in antes.items()},
                             "depois": {str(k): v for k, v in depois.items()}, "mudou": pc["situacao_valor"] != 1}
            salvar()

        # 2) estoque: o que o Bling não lançou sozinho ao atender, entra pela NOTA (POST /nfe/{id}/lancar-estoque/Local),
        #    medindo o saldo antes/depois. Se a nota não puder (itens da nota diferentes, lote, recusa), produto a produto.
        est = res.setdefault("estoque", {})
        res["_etapa"] = "estoque"; _ent_estado(nid, "estoque")
        pend = [x for x in p["itens"] if str(x["item_id"]) not in est and x["pid"] and x["qtd"] > 0]
        ini = res.get("saldo_inicio") or {}
        for x in pend:                       # entrou estoque entre o começo e o pedido (ex.: lançado ao importar a nota)
            if str(x["pid"]) not in ini:
                continue
            qtd_prod = sum(y["qtd"] for y in p["itens"] if y["pid"] == x["pid"])
            ext = float(res["pedido"]["antes"].get(str(x["pid"]), 0)) - float(ini[str(x["pid"])])
            if abs(ext - qtd_prod) < 0.001:
                est[str(x["item_id"])] = {"via": "importacao", "qtd": x["qtd"], "pid": x["pid"]}
                _ent_log(nid, f"estoque item {x['n_item']}", True, None, f"já entrou {qtd_prod:g} no Bling ao importar a nota: não lanço de novo", por=quem)
        pend = [x for x in pend if str(x["item_id"]) not in est]
        delta_at = {}
        for x in pend:
            a = float(res["pedido"]["antes"].get(str(x["pid"]), 0)); d = float(res["pedido"]["depois"].get(str(x["pid"]), 0))
            delta_at[int(x["pid"])] = d - a
        if pend and not res.get("nf_estoque") and all(abs(v) < 0.001 for v in delta_at.values()):  # noqa: E501
            codigos_nf = {}
            for it in res["nota"].get("itens") or []:
                k = str(it.get("codigo") or "").strip().upper()
                codigos_nf[k] = codigos_nf.get(k, 0.0) + float(it.get("qtd") or 0)
            precisa = {}
            for x in p["itens"]:
                k = str(x["sku"] or "").strip().upper()
                precisa[k] = precisa.get(k, 0.0) + float(x["qtd"] or 0)
            bate = all(abs(codigos_nf.get(k, -1) - q) < 0.001 for k, q in precisa.items()) and len(codigos_nf) == len(precisa)
            if not bate:
                res["nf_estoque"] = {"via": "produto", "motivo": "os itens da nota no Bling não batem com os SKUs/quantidades conferidos"}
                _ent_log(nid, "estoque pela nota", True, None, "itens da nota no Bling diferentes dos conferidos: lanço produto a produto", por=quem)
            else:
                antes2 = _saldos_local(ids)
                st, txt, _ = _bling_raw("POST", f"/nfe/{nf_id}/lancar-estoque/{DEP_LOCAL_ID}")
                ok = st in (200, 201, 204)
                depois2 = _saldos_local(ids)
                for _ in range(3):
                    if not ok or all(abs(depois2.get(k, 0) - antes2.get(k, 0)) >= 0.001 for k in ids):
                        break
                    time.sleep(3)
                    depois2 = _saldos_local(ids)
                ja = (not ok) and _ja_lancado(txt)
                res["nf_estoque"] = {"http": st, "ok": ok, "ja": ja, "antes": {str(k): v for k, v in antes2.items()},
                                     "depois": {str(k): v for k, v in depois2.items()}}
                _ent_log(nid, f"estoque da nota no Local", ok or ja, st,
                         "estoque da nota lançado no Local" if ok else ("o estoque da nota já estava lançado" if ja else _msg_bl(txt)), txt, por=quem)
            salvar()
        nfe = res.get("nf_estoque") or {}
        for x in pend:
            chave = str(x["item_id"])
            if chave in est or abs(delta_at.get(int(x["pid"]), 0)) >= 0.001:
                continue
            qtd_prod = sum(y["qtd"] for y in p["itens"] if y["pid"] == x["pid"])
            if nfe.get("ok"):
                d2 = float(nfe["depois"].get(str(x["pid"]), 0)) - float(nfe["antes"].get(str(x["pid"]), 0))
                if abs(d2 - qtd_prod) < 0.001:
                    est[chave] = {"via": "nota", "qtd": x["qtd"], "pid": x["pid"]}
                    continue
                if abs(d2) >= 0.001:
                    salvar()
                    raise RuntimeError(f"CONF|Estoque do item {x['n_item']} mudou {d2:+g} ao lançar a nota (esperado {qtd_prod:g}). "
                                       "Confira o estoque desse produto no Bling antes de continuar.")
            elif nfe.get("ja"):
                est[chave] = {"via": "importacao", "qtd": x["qtd"], "pid": x["pid"]}
                continue
        salvar()
        for x in p["itens"]:
            chave = str(x["item_id"])
            if chave in est or not x["pid"] or x["qtd"] <= 0:
                continue
            a = float(res["pedido"]["antes"].get(str(x["pid"]), 0)); d = float(res["pedido"]["depois"].get(str(x["pid"]), 0))
            # produto repetido em 2 itens: a medição é do produto inteiro
            qtd_prod = sum(y["qtd"] for y in p["itens"] if y["pid"] == x["pid"])
            if abs((d - a) - qtd_prod) < 0.001:
                est[chave] = {"via": "bling", "qtd": x["qtd"]}
                _ent_log(nid, f"estoque item {x['n_item']}", True, None, f"o Bling já lançou {x['qtd']:g} ao atender o pedido", por=quem)
            elif abs(d - a) < 0.001:
                corpo = {"produto": {"id": int(x["pid"])}, "deposito": {"id": DEP_LOCAL_ID}, "operacao": "E", "quantidade": x["qtd"],
                         "preco": x["custo"], "custo": x["custo"], "observacoes": f"Entrada NF {r['numero']} (pedido {pc['numero']}) conferida no WMS"}
                st, txt, js = _bling_raw("POST", "/estoques", corpo)
                ok = st in (200, 201)
                _ent_log(nid, f"estoque item {x['n_item']}", ok, st, f"+{x['qtd']:g} no Local" if ok else _msg_bl(txt), txt, corpo, por=quem)
                if not ok:
                    salvar()
                    raise RuntimeError(f"BLING|O Bling recusou lançar o estoque do item {x['n_item']}: {_msg_bl(txt) or st}")
                est[chave] = {"via": "wms", "qtd": x["qtd"], "id": ((js or {}).get("data") or {}).get("id"), "pid": x["pid"]}
            else:
                salvar()
                raise RuntimeError(f"CONF|Estoque do item {x['n_item']} mudou {d - a:+g} ao atender o pedido (esperado {qtd_prod:g} ou 0). "
                                   "Pode ter havido venda ao mesmo tempo: confira o estoque desse produto no Bling antes de continuar.")
            salvar()

        # 3) lotes (só produto com controle de lote ligado; não liga controle de ninguém)
        lts = res.setdefault("lotes", {})
        res["_etapa"] = "lotes"; _ent_estado(nid, "lotes")
        for x in p["itens"]:
            chave = str(x["item_id"])
            if chave in lts or not x["controla_lote"]:
                continue
            st, txt, js = _bling_raw("GET", "/produtos/lotes", params={"idsProdutos[]": x["pid"]})
            exist = {str(l.get("codigoLote")): l.get("idLote") for l in ((js or {}).get("data") or [])}
            idl = exist.get(x["lote"])
            if not idl:
                if not x.get("fabricacao"):
                    salvar()
                    raise RuntimeError(f"CONF|Falta a data de fabricação do lote {x['lote']} (item {x['n_item']}): o Bling exige para criar o lote. "
                                       "Informe em 'Ver o que vai ser lançado' e clique Continuar a entrada.")
                corpo = [{"produto": {"id": int(x["pid"])}, "deposito": {"id": DEP_LOCAL_ID}, "codigoLote": x["lote"],
                          "dataFabricacao": x["fabricacao"]}]
                if x.get("validade"):
                    corpo[0]["dataValidade"] = x["validade"]       # validade indeterminada: lote sem data de validade
                st, txt, js = _bling_raw("PUT", "/produtos/lotes", corpo)
                try:
                    idl = js["data"]["saved"][0]["id"]
                except Exception:
                    idl = None
                _ent_log(nid, f"lote {x['lote']} (item {x['n_item']})", bool(idl), st, "lote criado" if idl else _msg_bl(txt), txt, corpo, por=quem)
                if not idl:
                    salvar()
                    raise RuntimeError(f"BLING|O Bling recusou criar o lote {x['lote']} do item {x['n_item']}: {_msg_bl(txt) or st}")
            corpo = {"quantidade": x["qtd"], "tipoLancamento": 1, "observacao": f"Entrada NF {r['numero']} (WMS)"}
            st, txt, js = _bling_raw("POST", f"/produtos/lotes/{idl}/lancamentos", corpo)
            ok = st in (200, 201, 204)
            _ent_log(nid, f"lote {x['lote']} +{x['qtd']:g} (item {x['n_item']})", ok, st, "quantidade lançada no lote" if ok else _msg_bl(txt), txt, corpo, por=quem)
            if not ok:
                salvar()
                raise RuntimeError(f"BLING|O Bling recusou lançar a quantidade no lote {x['lote']}: {_msg_bl(txt) or st}")
            lts[chave] = {"id_lote": idl, "qtd": x["qtd"], "lote": x["lote"]}
            salvar()

        # FEFO da separação: atualiza já a cópia local dos lotes desses SKUs (senão espera a sync de hora em hora)
        skus_lote = sorted({str(x["sku"]) for x in p["itens"] if x.get("controla_lote") and x.get("sku")})
        if skus_lote and os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fefo_sync.py")):
            try:
                import subprocess
                import sys as _sys
                subprocess.Popen([_sys.executable, "fefo_sync.py", "--skus", ",".join(skus_lote), "--forcar"],
                                 cwd=os.path.dirname(os.path.abspath(__file__)),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception as e:
                print(f"[RECEB] fefo_sync não rodou: {e}", flush=True)

        # 4) contas a pagar: primeiro confere se já existem (mesmo fornecedor, vencimento e valor); senão lança as DA NOTA
        #    (POST /nfe/{id}/lancar-contas); se a nota não tiver parcelas ou o Bling recusar, uma por duplicata do XML
        cts = res.setdefault("contas", {})
        res["_etapa"] = "contas"; _ent_estado(nid, "contas")
        if not cts and p["contas"] and not res.get("contas_via"):
            forn = pc.get("fornecedor_id")
            exist = _contas_existentes(forn, p["contas"])
            if len(exist) == len(p["contas"]):
                cts.update(exist); res["contas_via"] = "existentes"
                _ent_log(nid, "contas a pagar", True, None, f"as {len(exist)} contas já existiam no Bling: não lancei de novo", por=quem)
            elif not exist and res["nota"].get("parcelas"):
                st, txt, _ = _bling_raw("POST", f"/nfe/{nf_id}/lancar-contas")
                ok = st in (200, 201, 204)
                ja = (not ok) and _ja_lancado(txt)
                _ent_log(nid, "contas da nota", ok or ja, st, "contas da nota lançadas" if ok else ("as contas da nota já estavam lançadas" if ja else _msg_bl(txt)), txt, por=quem)
                if ok or ja:
                    time.sleep(2)
                    achou = _contas_existentes(forn, p["contas"])
                    cts.update({k: dict(v, via="nota") for k, v in achou.items()})
                    res["contas_via"] = "nota" if ok else "existentes"
                    if not achou:
                        cts["nota"] = {"via": "nota", "valor": round(sum(c["valor"] for c in p["contas"]), 2)}
            salvar()
        for k, ct in enumerate(p["contas"]):
            if res.get("contas_via") in ("nota", "existentes"):
                break
            chave = str(ct["numero"] or k + 1)
            if chave in cts:
                continue
            st, txt, js = _bling_raw("POST", "/contas/pagar", ct["payload"])
            idc = ((js or {}).get("data") or {}).get("id")
            ok = st in (200, 201) and idc
            _ent_log(nid, f"conta a pagar {chave} ({_brl(ct['valor'])}, vence {ct['vencimento']})", bool(ok), st,
                     "conta lançada" if ok else _msg_bl(txt), txt, ct["payload"], por=quem)
            if not ok:
                salvar()
                raise RuntimeError(f"BLING|O Bling recusou a conta a pagar {chave}: {_msg_bl(txt) or st}")
            cts[chave] = {"id": idc, "valor": ct["valor"], "vencimento": ct["vencimento"]}
            salvar()

        res["_etapa"] = "ok"
        n_est = sum(1 for v in est.values() if v.get("via") == "wms")
        n_nota = sum(1 for v in est.values() if v.get("via") in ("nota", "importacao"))
        vin = res.get("vinculo") or {}
        msg = (f"Entrada lançada no Bling: NF {r['numero']} conciliada com o pedido {pc['numero']}" if vin.get("ok")
               else f"Entrada lançada no Bling (sem conciliar com o pedido {pc['numero']}: {str(vin.get('msg') or '')[:120]})")
        msg += (f" · pedido Atendido · estoque de {len(est)} item(ns) ({n_nota} pela nota, {n_est} produto a produto, "
                f"{len(est) - n_nota - n_est} pelo pedido) · {len(lts)} lote(s) · {len([c for c in cts if c != 'nota']) or len(cts)} conta(s) a pagar"
                + (" (da nota)" if res.get("contas_via") == "nota" else " (já existiam)" if res.get("contas_via") == "existentes" else "") + ".")
        _ent_estado(nid, "ok", entrada_resumo=res, entrada_msg=msg[:500], entrada_erro=None)
        with db_session() as c:
            c.execute("UPDATE receb_notas SET status='lancada', atualizado_em=? WHERE id=?", (_agora(), nid))
        _ent_log(nid, "concluído", True, None, msg, por=quem)
    except Exception as e:
        import traceback
        t = str(e)
        if t.startswith("BLING|") or t.startswith("CONF|") or t.startswith("VINC|"):
            org = {"BLING": "bling", "CONF": "conferencia", "VINC": "vinculo"}[t.split("|", 1)[0]]
            msg = t.split("|", 1)[1]
        else:
            print(f"[ENTRADA] {nid}: {traceback.format_exc()}", flush=True)
            org, msg = "wms", f"Erro interno do WMS ({type(e).__name__}): {t[:300]}"
        _ent_log(nid, "parou", False, None, msg, por=quem)
        _ent_estado(nid, "erro", entrada_erro=json.dumps({"origem": org, "error": msg}, ensure_ascii=False), entrada_msg=msg[:500])
    finally:
        with _ent_lock:
            _ent_ativos.discard(nid)


def _disparar_entrada(nid, quem):
    with _ent_lock:
        if nid in _ent_ativos:
            return False
        _ent_ativos.add(nid)
    threading.Thread(target=_job_entrada, args=(nid, quem), daemon=True).start()
    return True


@recebimento_routes.get("/recebimento/notas/<int:nid>/entrada")
@login_required
def entrada_info(nid):
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT * FROM receb_notas WHERE id=?", (nid,)).fetchone()
        if not r:
            return jsonify({"error": "Nota não encontrada."}), 404
        log = [dict(x) for x in c.execute("SELECT id, acao, ok, http, mensagem, resposta, enviado, por, em FROM receb_entrada_log "
                                          "WHERE nota_id=? ORDER BY id DESC LIMIT 60", (nid,)).fetchall()]
    erro = None
    try:
        erro = json.loads(r["entrada_erro"]) if r["entrada_erro"] else None
    except Exception:
        pass
    out = {"etapa": r["entrada_etapa"], "etapa_txt": ETAPAS_ENT.get(r["entrada_etapa"] or "", ""), "rodando": _ent_rodando(r),
           "msg": r["entrada_msg"], "erro": erro if r["entrada_etapa"] == "erro" else None, "por": r["entrada_por"],
           "resumo": _ent_resumo(r), "historico": log, "auto": ENTRADA_AUTO(), "status": r["status"]}
    if request.args.get("previa") and r["entrada_etapa"] not in ("ok",) and not out["rodando"]:
        try:
            p = plano_entrada(nid)
            out["previa"] = {"pontos": p["pontos"], "bloqueio": p["bloqueio"], "contas": [{k: v for k, v in x.items() if k != "payload"} for x in p["contas"]],
                             "lotes": [{k: x.get(k) for k in ("item_id", "n_item", "sku", "descricao", "qtd", "lote", "validade", "fabricacao",
                                                              "fabricacao_origem", "lote_existe")} for x in p["itens"] if x.get("controla_lote")]}
        except Exception as e:
            out["previa"] = {"pontos": [], "bloqueio": f"Erro ao montar a prévia: {e}"}
    return jsonify(out)


@recebimento_routes.post("/recebimento/notas/<int:nid>/entrada")
@login_required
def entrada_lancar(nid):
    """Dá entrada no Bling (supervisor/admin). Roda em segundo plano; a tela acompanha por GET .../entrada."""
    if not eh_gestor():
        return jsonify({"error": "Só supervisor ou admin dá entrada no Bling.", "origem": "conferencia"}), 403
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT * FROM receb_notas WHERE id=?", (nid,)).fetchone()
    if not r:
        return jsonify({"error": "Nota não encontrada."}), 404
    if r["entrada_etapa"] == "ok":
        return jsonify({"error": "A entrada dessa nota já foi lançada no Bling.", "origem": "conferencia"}), 409
    if r["status"] != "conferida":
        return jsonify({"error": "Só nota CONFERIDA entra no Bling.", "origem": "conferencia"}), 409
    if (request.get_json(silent=True) or {}).get("sem_vinculo") and not _ent_rodando(r):
        res = _ent_resumo(r)
        if not (res.get("vinculo") or {}).get("ok"):
            res["vinculo"] = {"ok": False, "pulado": True, "msg": f"seguiu sem conciliar com o pedido (decisão de {_quem()})"}
            with db_session() as c:
                c.execute("UPDATE receb_notas SET entrada_resumo=? WHERE id=?", (json.dumps(res, ensure_ascii=False), nid))
            _ent_log(nid, "conciliação nota x pedido", False, None, res["vinculo"]["msg"], por=_quem())
    if not _disparar_entrada(nid, _quem()):
        return jsonify({"success": True, "andamento": True}), 202
    return jsonify({"success": True, "andamento": True}), 202


@recebimento_routes.post("/recebimento/notas/<int:nid>/entrada/desfazer")
@login_required
def entrada_desfazer(nid):
    """Desfaz o que o WMS lançou (supervisor/admin): apaga as contas, tira do lote, tira o estoque que o WMS pôs e volta o pedido."""
    if not eh_gestor():
        return jsonify({"error": "Só supervisor ou admin desfaz a entrada.", "origem": "conferencia"}), 403
    garantir_tabelas()
    with db_session() as c:
        r = c.execute("SELECT * FROM receb_notas WHERE id=?", (nid,)).fetchone()
    if not r:
        return jsonify({"error": "Nota não encontrada."}), 404
    if _ent_rodando(r):
        return jsonify({"error": "A entrada ainda está rodando. Espere terminar.", "origem": "conferencia"}), 409
    res, quem, falhas = _ent_resumo(r), _quem(), []
    _ent_estado(nid, "desfazendo")
    nf_id = (res.get("nota") or {}).get("id")
    if res.get("contas_via") == "nota" and nf_id:
        st, txt, _ = _bling_raw("POST", f"/nfe/{nf_id}/estornar-contas")
        ok = st in (200, 201, 204)
        _ent_log(nid, "desfazer contas da nota", ok, st, "contas da nota estornadas" if ok else _msg_bl(txt), txt, por=quem)
        if ok:
            res["contas"] = {}; res.pop("contas_via", None)
        else:
            falhas.append(f"contas da nota: {_msg_bl(txt) or st}")
    elif res.get("contas_via") == "existentes":
        res["contas"] = {}; res.pop("contas_via", None)       # não foram lançadas pelo WMS: ficam
    for k, ct in list((res.get("contas") or {}).items()):
        st, txt, _ = _bling_raw("DELETE", f"/contas/pagar/{ct['id']}")
        ok = st in (200, 204)
        _ent_log(nid, f"desfazer conta {k}", ok, st, "conta apagada" if ok else _msg_bl(txt), txt, por=quem)
        if ok:
            res["contas"].pop(k)
        else:
            falhas.append(f"conta {k}: {_msg_bl(txt) or st}")
    for k, lt in list((res.get("lotes") or {}).items()):
        corpo = {"quantidade": lt["qtd"], "tipoLancamento": 2, "observacao": f"Desfeito entrada NF {r['numero']} (WMS)"}
        st, txt, _ = _bling_raw("POST", f"/produtos/lotes/{lt['id_lote']}/lancamentos", corpo)
        ok = st in (200, 201, 204)
        _ent_log(nid, f"desfazer lote {lt['lote']}", ok, st, "tirado do lote" if ok else _msg_bl(txt), txt, corpo, por=quem)
        if ok:
            res["lotes"].pop(k)
        else:
            falhas.append(f"lote {lt['lote']}: {_msg_bl(txt) or st}")
    if nf_id and any(e.get("via") == "nota" for e in (res.get("estoque") or {}).values()):
        st, txt, _ = _bling_raw("POST", f"/nfe/{nf_id}/estornar-estoque")
        ok = st in (200, 201, 204)
        _ent_log(nid, "desfazer estoque da nota", ok, st, "estoque da nota estornado" if ok else _msg_bl(txt), txt, por=quem)
        if ok:
            res["estoque"] = {k: e for k, e in res["estoque"].items() if e.get("via") != "nota"}
            res.pop("nf_estoque", None)
        else:
            falhas.append(f"estoque da nota: {_msg_bl(txt) or st}")
    for k, e in list((res.get("estoque") or {}).items()):
        if e.get("via") != "wms":
            continue
        corpo = {"produto": {"id": int(e["pid"])}, "deposito": {"id": DEP_LOCAL_ID}, "operacao": "S", "quantidade": e["qtd"],
                 "observacoes": f"Desfeito entrada NF {r['numero']} (WMS)"}
        st, txt, _ = _bling_raw("POST", "/estoques", corpo)
        ok = st in (200, 201)
        _ent_log(nid, f"desfazer estoque item {k}", ok, st, f"-{e['qtd']:g} no Local" if ok else _msg_bl(txt), txt, corpo, por=quem)
        if ok:
            res["estoque"].pop(k)
        else:
            falhas.append(f"estoque item {k}: {_msg_bl(txt) or st}")
    ped = res.get("pedido") or {}
    if ped.get("mudou") and ped.get("anterior_id") and r["pedido_compra_id"]:
        st, txt, _ = _bling_raw("PATCH", f"/pedidos/compras/{r['pedido_compra_id']}/situacoes/{ped['anterior_id']}")
        ok = st in (200, 204)
        _ent_log(nid, "desfazer pedido (situação anterior)", ok, st, "pedido voltou à situação anterior" if ok else _msg_bl(txt), txt, por=quem)
        if ok:
            res.pop("pedido", None)
            pendente_bling = [k for k, e in (res.get("estoque") or {}).items() if e.get("via") == "bling"]
            if pendente_bling:
                falhas.append("o estoque que o Bling lançou ao atender o pedido: confira se saiu ao voltar a situação (itens "
                              + ", ".join(pendente_bling) + ")")
        else:
            falhas.append(f"pedido: {_msg_bl(txt) or st}")
    vin = res.get("vinculo") or {}
    if vin.get("ok") and vin.get("robo"):
        _ent_log(nid, "conciliação nota x pedido", True, None, "continua no Bling. Para tirar: Notas de entrada > marcar a nota > "
                 "Desvincular pedido compra (antes de lançar o estoque de novo)", por=quem)
    if not falhas:
        for k in ("nf_estoque", "saldo_inicio", "contas_via"):
            res.pop(k, None)
    res["_etapa"] = "desfeito" if not falhas else "erro"
    msg = ("Entrada desfeita no Bling (a nota continua importada lá, sem estoque nem contas)." if not falhas
           else "Desfeito em parte. Falta: " + "; ".join(falhas))
    _ent_estado(nid, "desfeito" if not falhas else "erro", entrada_resumo=res, entrada_msg=msg[:500],
                entrada_erro=None if not falhas else json.dumps({"origem": "bling", "error": msg}, ensure_ascii=False))
    if not falhas:
        with db_session() as c:
            c.execute("UPDATE receb_notas SET status='conferida', atualizado_em=? WHERE id=? AND status='lancada'", (_agora(), nid))
    return jsonify({"success": not falhas, "msg": msg}), (200 if not falhas else 422)
