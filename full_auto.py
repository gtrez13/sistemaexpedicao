"""FULL automático (Biel 16/11: "eu preciso que seja automático a lista"; escolheu "os dois", cobertura de 45 dias).

O ML não deixa ler a remessa criada no painel, então a lista nasce de dois jeitos sem ninguém digitar:

1) SUGESTÃO DO WMS  (GET /full/sugestao, POST /full/sugestao/calcular)
   Para cada anúncio Full (por código do Full = inventory_id):
     vendas dos últimos FULL_JANELA_DIAS (30) dias no ML  -> venda por dia
     estoque no Full agora (/inventories/{id}/stock/fulfillment, disponível)
     a caminho = remessas do WMS ainda não enviadas + enviadas nos últimos FULL_A_CAMINHO_DIAS (7) dias
     galpão = saldo físico no Bling (depósito da LHVMed)
   manda = venda/dia x FULL_COBERTURA_DIAS (45) - no Full - a caminho, nunca mais do que tem no galpão.
   Roda em segundo plano (leva alguns minutos na 1ª vez) e fica guardado em full_sugestao; recalcula sozinho a cada 6 h
   quando alguém abre a tela, ou no botão "Recalcular".

2) ARQUIVO DO PAINEL  (POST /full/importar, multipart "arquivo")
   PDF de etiquetas, PDF/planilha do envio (xlsx/csv) que o painel do ML gera -> lê o texto, acha os códigos do Full
   (ou MLB / SKU) dos anúncios conhecidos e a quantidade: na etiqueta conta quantas vezes o código aparece; no resumo pega
   a quantidade da linha. Devolve a lista para conferir na tela antes de criar. Não grava nada."""
from __future__ import annotations

import io
import math
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from flask import jsonify, request
from flask_login import login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

COBERTURA = int(os.environ.get("FULL_COBERTURA_DIAS", "45") or 45)
JANELA = int(os.environ.get("FULL_JANELA_DIAS", "30") or 30)
A_CAMINHO_DIAS = int(os.environ.get("FULL_A_CAMINHO_DIAS", "7") or 7)
BRT = timezone(timedelta(hours=-3))
_st = {"rodando": False, "etapa": None, "feito": 0, "total": 0, "erro": None, "em": 0}
_lk = threading.Lock()
_ok_tab = False


def _tab():
    global _ok_tab
    if _ok_tab:
        return
    with db_session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS full_sugestao (inventory_id TEXT PRIMARY KEY, chave TEXT, sku TEXT, titulo TEXT, variacao TEXT, "
                  "foto TEXT, no_full INTEGER, vendas INTEGER, janela INTEGER, a_caminho INTEGER, galpao INTEGER, sugerido INTEGER, "
                  "limitado INTEGER, calculado_em BIGINT)")
    _ok_tab = True


def _etapa(nome, feito=0, total=0):
    with _lk:
        _st.update(etapa=nome, feito=feito, total=total)


def _deposito():
    return (os.environ.get("BLING_DEPOSITO_ID") or os.environ.get("BLING_DEPOSITO_LOCAL_ID") or "10488672491").strip()


# ───────────────────────── 1) sugestão ─────────────────────────
def _vendas(ml, chaves):
    """{chave: unidades vendidas na janela} pelos pedidos pagos do ML (em fatias de 3 dias para não estourar a paginação)."""
    out = {}
    fim = datetime.now(BRT)
    ini = fim - timedelta(days=JANELA)
    fatias = []
    t = ini
    while t < fim:
        fatias.append((t, min(t + timedelta(days=3), fim)))
        t += timedelta(days=3)
    for n, (a, b) in enumerate(fatias):
        _etapa("Vendas dos últimos dias no Mercado Livre", n, len(fatias))
        off = 0
        while True:
            r = ml._request("GET", "/orders/search", params={
                "seller": ml.user_id, "order.status": "paid", "sort": "date_asc", "limit": 50, "offset": off,
                "order.date_created.from": a.strftime("%Y-%m-%dT%H:%M:%S.000-03:00"),
                "order.date_created.to": b.strftime("%Y-%m-%dT%H:%M:%S.000-03:00")})
            if r is None or r.status_code != 200:
                raise RuntimeError(f"pedidos do ML: HTTP {getattr(r, 'status_code', 'sem resposta')}")
            d = r.json() or {}
            res = d.get("results") or []
            for o in res:
                for oi in o.get("order_items") or []:
                    it = oi.get("item") or {}
                    iid, vid = it.get("id"), it.get("variation_id")
                    k = f"{iid}:{vid}" if vid else iid
                    if k not in chaves and iid in chaves:
                        k = iid
                    if k in chaves:
                        out[k] = out.get(k, 0) + int(oi.get("quantity") or 0)
            off += len(res)
            if not res or off >= int((d.get("paging") or {}).get("total") or 0) or off >= 9950:
                break
            time.sleep(0.1)
    return out


def _no_full(ml, invs):
    out = {}
    for n, inv in enumerate(invs):
        if n % 10 == 0:
            _etapa("Estoque no Full", n, len(invs))
        r = ml._request("GET", f"/inventories/{inv}/stock/fulfillment")
        if r is not None and r.status_code == 200:
            out[inv] = int((r.json() or {}).get("available_quantity") or 0)
        time.sleep(0.08)
    return out


def _galpao(skus):
    """{sku: saldo físico no depósito} pelo Bling (só leitura, em lote de 50, com o id guardado em estoque_cache)."""
    import estoque_sync as E
    dep = _deposito()
    out, ids = {}, {}
    with db_session() as c:
        E.garantir_tabela(c)
        for s in skus:
            r = c.execute("SELECT id_bling FROM estoque_cache WHERE sku_key=?", (s.lower(),)).fetchone()
            if r and r["id_bling"]:
                ids[s] = int(r["id_bling"])
    b = E.Bling()
    try:
        falta = [s for s in skus if s not in ids]
        for n, s in enumerate(falta):
            if n % 10 == 0:
                _etapa("Estoque do galpão no Bling (1ª vez de cada produto)", n, len(falta))
            r = b.get("/produtos", {"codigo": s, "limite": 5}) or {}
            p = next((p for p in (r.get("data") or []) if str(p.get("codigo", "")).lower() == s.lower()), None)
            if not p:
                continue
            ids[s] = int(p["id"])
            with db_session() as c:
                c.execute("INSERT INTO estoque_cache (sku_key, id_bling, atualizado_em) VALUES (?,?,?) "
                          "ON CONFLICT (sku_key) DO UPDATE SET id_bling=EXCLUDED.id_bling", (s.lower(), ids[s], int(time.time())))
        por_id = {v: k for k, v in ids.items()}
        lista = list(por_id)
        for i in range(0, len(lista), 50):
            _etapa("Estoque do galpão no Bling", i, len(lista))
            r = b.get("/estoques/saldos", [("idsProdutos[]", str(x)) for x in lista[i:i + 50]]) or {}
            for row in r.get("data") or []:
                pid = (row.get("produto") or {}).get("id")
                if pid is not None and int(pid) in por_id:
                    out[por_id[int(pid)]] = E.saldo_deposito(row, dep)
    except E.Parar as e:                      # 429/token: segue sem o galpão dos que faltaram (não limita esses)
        print(f"⚠️ full sugestão: Bling parou: {e}", flush=True)
    return out


def _a_caminho():
    """{inventory_id: unidades} em remessas do WMS ainda não enviadas ou enviadas há poucos dias (o Full ainda não recebeu)."""
    out = {}
    lim = int(time.time()) - A_CAMINHO_DIAS * 86400
    with db_session() as c:
        rows = c.execute("SELECT i.inventory_id, i.qtd, i.separado, r.status FROM full_itens i JOIN full_remessas r ON r.id=i.remessa_id "
                         "WHERE r.status IN ('aberta','separando','separada') OR (r.status='enviada' AND COALESCE(r.fechado_em, r.atualizado_em, 0) > ?)",
                         (lim,)).fetchall()
    for r in rows:
        if r["inventory_id"]:
            q = int(r["separado"] or 0) if r["status"] == "enviada" else int(r["qtd"] or 0)
            out[r["inventory_id"]] = out.get(r["inventory_id"], 0) + q
    return out


def calcular():
    with _lk:
        if _st["rodando"]:
            return
        _st.update(rodando=True, erro=None, etapa="Começando", feito=0, total=0)
    try:
        import full_routes as F
        _tab()
        with db_session() as c:
            ult = c.execute("SELECT MAX(atualizado_em) AS m FROM full_anuncios").fetchone()["m"]
        if not ult or time.time() - int(ult) > 6 * 3600:
            _etapa("Lendo os anúncios Full do ML")
            F._atualizar_anuncios()
        with db_session() as c:
            an = [dict(r) for r in c.execute("SELECT * FROM full_anuncios WHERE COALESCE(status,'') <> 'closed'").fetchall()]
        chaves = {a["chave"]: a for a in an}
        for a in an:                              # anúncio sem variação também atende pelo MLB
            if not a["variation_id"]:
                chaves.setdefault(a["item_id"], a)
        from services.ml_service import MercadoLivreService
        ml = MercadoLivreService()
        vend = _vendas(ml, chaves)
        por_inv = {}                              # vários anúncios podem dividir o mesmo estoque do Full
        for a in an:
            inv = a["inventory_id"]
            if not inv:
                continue
            g = por_inv.setdefault(inv, {"a": a, "vendas": 0})
            if a["status"] == "active" and g["a"]["status"] != "active":
                g["a"] = a
            g["vendas"] += vend.get(a["chave"], 0)
        full = _no_full(ml, list(por_inv))
        skus = sorted({g["a"]["sku"] for g in por_inv.values() if g["a"]["sku"]})
        galp = _galpao(skus)
        cam = _a_caminho()
        agora = int(time.time())
        linhas = []
        for inv, g in por_inv.items():
            a = g["a"]
            vdia = g["vendas"] / JANELA
            nf = full.get(inv)
            precisa = max(0, math.ceil(vdia * COBERTURA) - (nf or 0) - cam.get(inv, 0)) if nf is not None else 0
            gp = galp.get(a["sku"]) if a["sku"] else None
            sug = min(precisa, max(0, gp)) if gp is not None else precisa
            linhas.append((inv, a["chave"], a["sku"], a["titulo"], a["variacao"], a["foto"], nf, g["vendas"], JANELA, cam.get(inv, 0), gp,
                           sug, 1 if sug < precisa else 0, agora))
        with db_session() as c:
            c.execute("DELETE FROM full_sugestao")
            for x in linhas:
                c.execute("INSERT INTO full_sugestao (inventory_id, chave, sku, titulo, variacao, foto, no_full, vendas, janela, a_caminho, galpao, "
                          "sugerido, limitado, calculado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", x)
        with _lk:
            _st.update(em=agora, etapa=None)
    except Exception as e:
        print(f"⚠️ full sugestão falhou: {e}", flush=True)
        with _lk:
            _st["erro"] = str(e)[:200]
    finally:
        with _lk:
            _st["rodando"] = False


# ───────────────────────── 2) arquivo do painel ─────────────────────────
def _texto_xlsx(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    comp = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{ns}si"):
            comp.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
    linhas = []
    for nome in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet")):
        for row in ET.fromstring(z.read(nome)).iter(f"{ns}row"):
            cel = []
            for cc in row.iter(f"{ns}c"):
                v = cc.find(f"{ns}v")
                if cc.get("t") == "s" and v is not None:
                    cel.append(comp[int(v.text)])
                elif cc.get("t") == "inlineStr":
                    cel.append("".join(t.text or "" for t in cc.iter(f"{ns}t")))
                else:
                    cel.append(v.text if v is not None and v.text else "")
            linhas.append("\t".join(cel))
    return "\n".join(linhas), True


def _texto(nome, b):
    n = (nome or "").lower()
    if n.endswith(".pdf") or b[:4] == b"%PDF":
        from pypdf import PdfReader
        pdf = PdfReader(io.BytesIO(b))
        return "\n".join((p.extract_text() or "") for p in pdf.pages), False, len(pdf.pages)
    if n.endswith((".xlsx", ".xlsm")) or b[:2] == b"PK":
        t, tab = _texto_xlsx(b)
        return t, tab, None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return b.decode(enc), True, None
        except UnicodeDecodeError:
            pass
    return "", True, None


_RE_QTD = re.compile(r"(?:quantidade|qtd\.?|qtde|unidades|un\.?)\s*[:=]?\s*(\d{1,5})|(\d{1,5})\s*(?:unidades|un\b|unid)", re.I)


def ler_arquivo(nome, b):
    texto, tabela, paginas = _texto(nome, b)
    with db_session() as c:
        an = [dict(r) for r in c.execute("SELECT * FROM full_anuncios WHERE COALESCE(status,'') <> 'closed'").fetchall()]
    idx = {}                                      # código -> (prioridade, anúncio); inventory_id > MLB > SKU
    for a in an:
        for pr, cod in ((0, a["inventory_id"]), (1, a["item_id"]), (2, a["sku"])):
            if cod:
                k = str(cod).strip().upper()
                if k not in idx or idx[k][0] > pr:
                    idx[k] = (pr, a)
    linhas = texto.splitlines()
    achado = {}                                   # chave -> {pr: [ocorrências], qtd_linha}
    for ln in linhas:
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-/]*", ln)
        melhor = {}                               # um anúncio conta 1 vez por linha, pelo melhor código (Full > MLB > SKU)
        for tk in toks:
            k = tk.strip(".-/").upper()
            if k in idx:
                pr, a = idx[k]
                if a["chave"] not in melhor or melhor[a["chave"]][0] > pr:
                    melhor[a["chave"]] = (pr, a, k)
        codigos = {m[2] for m in melhor.values()}
        for pr, a, k in melhor.values():
            e = achado.setdefault(a["chave"], {"a": a, "oc": {0: 0, 1: 0, 2: 0}, "qtd": None, "cod": {}})
            e["oc"][pr] += 1
            e["cod"][pr] = k
            q = None
            if tabela:                            # planilha: quantidade = último número da linha que não é código
                nums = [int(x) for x in re.findall(r"(?<![\w.,])(\d{1,5})(?![\w.,])", ln) if x.upper() not in codigos]
                q = nums[-1] if nums else None
            else:
                m = _RE_QTD.search(ln)
                q = int(m.group(1) or m.group(2)) if m else None
            if q:
                e["qtd"] = (e["qtd"] or 0) + q
    # etiqueta (o mesmo código repete) = conta; resumo (aparece 1 vez com quantidade) = quantidade da linha
    repetidos = sum(1 for e in achado.values() if max(e["oc"].values()) > 1)
    modo = "etiquetas" if (not tabela and repetidos >= max(1, len(achado) // 2)) else "resumo"
    itens = []
    for ch, e in achado.items():
        pr = min(p for p, v in e["oc"].items() if v)
        if modo == "etiquetas":
            q, conferir = e["oc"][pr], False
        else:
            q, conferir = (e["qtd"], False) if e["qtd"] else (e["oc"][pr], True)
        itens.append({"chave": ch, "qtd": int(q), "conferir": conferir, "codigo": e["cod"][pr], "titulo": e["a"]["titulo"],
                      "variacao": e["a"]["variacao"], "inventory_id": e["a"]["inventory_id"], "sku": e["a"]["sku"]})
    itens.sort(key=lambda x: str(x["titulo"]))
    return {"modo": modo, "itens": itens, "unidades": sum(x["qtd"] for x in itens), "paginas": paginas,
            "linhas_lidas": len(linhas), "vazio": not texto.strip()}


def registrar(bp):
    @bp.get("/full/sugestao")
    @login_required
    def full_sugestao():
        _tab()
        with db_session() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM full_sugestao ORDER BY sugerido DESC, titulo").fetchall()]
        calc = max((r["calculado_em"] or 0 for r in rows), default=0)
        if not _st["rodando"] and (not rows or time.time() - calc > 6 * 3600) and not request.args.get("so_ler"):
            threading.Thread(target=calcular, daemon=True).start()
        with _lk:
            st = dict(_st)
        cam = _a_caminho() if rows else {}
        for r in rows:                            # remessa criada depois do cálculo já desconta na hora
            extra = max(0, cam.get(r["inventory_id"], 0) - int(r["a_caminho"] or 0))
            if extra:
                r["a_caminho"] = int(r["a_caminho"] or 0) + extra
                r["sugerido"] = max(0, int(r["sugerido"] or 0) - extra)
        return jsonify({"itens": [r for r in rows if r["sugerido"] > 0], "analisados": len(rows), "calculado_em": calc or None,
                        "cobertura": COBERTURA, "janela": JANELA, "status": st})

    @bp.post("/full/sugestao/calcular")
    @login_required
    def full_sugestao_calcular():
        _tab()
        threading.Thread(target=calcular, daemon=True).start()
        time.sleep(0.3)
        with _lk:
            return jsonify({"status": dict(_st)})

    @bp.post("/full/importar")
    @login_required
    def full_importar():
        f = request.files.get("arquivo")
        if not f:
            return jsonify({"error": "Mande o arquivo (PDF, planilha ou CSV)."}), 400
        b = f.read()
        if len(b) > 30 * 1024 * 1024:
            return jsonify({"error": "Arquivo grande demais (máx. 30 MB)."}), 400
        try:
            r = ler_arquivo(f.filename, b)
        except Exception as e:
            return jsonify({"error": f"Não consegui ler o arquivo: {type(e).__name__}: {e}"[:300]}), 400
        if r["vazio"]:
            r["aviso"] = "O arquivo não tem texto (pode ser imagem escaneada): não dá para ler os códigos."
        elif not r["itens"]:
            r["aviso"] = "Não achei nenhum código de anúncio Full no arquivo. Atualize a lista do ML e tente de novo, ou me mande o arquivo."
        return jsonify(r)
