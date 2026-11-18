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
  POST /full/remessas/<id>/reimprimir    {item_id, qtd, imprime_aqui?}  N etiquetas de novo, com a mesma validade/lote (fila ou o aparelho)
  POST /full/remessas/<id>/validade      {item_id, validade?}  produto sem etiqueta do Full (código universal) que o ML exige validade:
                                                              etiquetas só de validade/lote, uma por unidade separada
  GET  /full/etiquetas/fila?estacao=     computador da Elgin reserva e recebe as etiquetas que faltam imprimir
  POST /full/etiquetas/impressas         {ids, estacao}        confirma as impressas (sem confirmar, voltam para a fila em 30 s)
  GET  /full/novas                       remessas abertas (aviso com som em qualquer tela)

Etiqueta: se o aparelho que bipou tem a Elgin (QZ), ele imprime na hora (imprime_aqui=true); senão o bip entra na fila e o
computador da Elgin (estação) imprime sozinho. Assim o separador pode bipar pelo celular."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

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
_estacao = {"nome": None, "em": 0}          # último computador que pediu a fila de etiquetas (o da Elgin)
RESERVA_S = 30                               # etiqueta reservada e não confirmada volta para a fila depois disso


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
                  "desfeito INTEGER NOT NULL DEFAULT 0, reimpressao INTEGER NOT NULL DEFAULT 0, impresso_em BIGINT, impresso_por TEXT, "
                  "reservado_por TEXT, reservado_em BIGINT)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_full_bips_fila ON full_bips (impresso_em, desfeito)")
        c.execute(f"CREATE TABLE IF NOT EXISTS full_atualizacoes (id {pk}, remessa_id BIGINT, em BIGINT, por TEXT, tipo TEXT, antes TEXT, mudancas TEXT)")
    _colunas()
    _ok_tab = True


COLS_NOVAS = {                                 # 16/11 tarde: frete do ML, kit (composição do Bling), lote/validade, identificação
    "full_remessas": [("frete", "TEXT"), ("data_envio", "TEXT"), ("origem", "TEXT")],
    "full_itens": [("identificacao", "TEXT"), ("instrucoes", "TEXT"), ("exige_validade", "INTEGER"), ("comp", "TEXT"), ("etiquetado", "INTEGER"), ("validade_etq", "TEXT")],
    "full_bips": [("lote", "TEXT"), ("validade", "TEXT"), ("detalhe", "TEXT"), ("fisico", "TEXT")],
}


def _colunas():
    """Acrescenta as colunas novas nas tabelas que já existem (cada uma na sua transação: no Postgres um erro aborta a transação)."""
    for t, cols in COLS_NOVAS.items():
        for col, tipo in cols:
            try:
                with db_session() as c:
                    c.execute(f"SELECT {col} FROM {t} LIMIT 1").fetchall()
                continue
            except Exception:
                pass
            try:
                with db_session() as c:
                    c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {tipo}")
            except Exception as e:
                print(f"⚠️ full: coluna {t}.{col}: {e}", flush=True)


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
VALIDADE_DIAS = int(os.environ.get("FULL_VALIDADE_DIAS", "90") or 90)   # ML: validade 90 dias depois da data de envio agendada


def _comp(it):
    """Kit (composição do Bling): [{sku, nome, q, ean, bip}]; [] = produto simples; None = ainda não conferido no Bling."""
    v = it.get("comp")
    if v is None:
        return None
    try:
        return json.loads(v) if v else []
    except Exception:
        return []


def _kits_feitos(comp, qtd):
    return min([int(c.get("bip") or 0) // max(1, int(c.get("q") or 1)) for c in comp] + [int(qtd)]) if comp else 0


def _limite_validade(rem):
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(str(rem.get("data_envio") or "")[:10])
    except ValueError:
        d = date.today() + timedelta(days=7)
    return (d + timedelta(days=VALIDADE_DIAS)).isoformat()


def _sobra(it, q):
    """Unidades já separadas além da quantidade q (PDF novo pediu menos ou tirou o produto): voltam para a prateleira."""
    if it.get("comp"):
        return sum(max(0, int(cp.get("bip") or 0) - int(cp["q"]) * q) for cp in it["comp"])
    return max(0, int(it.get("separado") or 0) - q)


def _mexeu(it):
    return int(it.get("separado") or 0) > 0 or int(it.get("etiquetado") or 0) > 0 or any(int(cp.get("bip") or 0) for cp in (it.get("comp") or []))


def _chave_full(x):
    inv = str(x.get("inventory_id") or "").strip().upper()
    return inv or f"CH:{x.get('chave')}"


def _rem(c, rid, com_itens=True):
    r = c.execute("SELECT * FROM full_remessas WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    its = [dict(x) for x in c.execute("SELECT * FROM full_itens WHERE remessa_id=? ORDER BY id", (rid,)).fetchall()]
    for it in its:
        it["comp"] = _comp(it)
        it["exige_validade"] = bool(it.get("exige_validade"))
        it["sobra"] = _sobra(it, int(it["qtd"] or 0))                          # PDF novo pediu menos: unidades para devolver
        it["etq_sobra"] = max(0, int(it.get("etiquetado") or 0) - int(it["qtd"] or 0))
    d["sobra"] = sum(x["sobra"] for x in its)
    d["etq_sobra"] = sum(x["etq_sobra"] for x in its)
    d["n_itens"] = len(its)
    d["unidades"] = sum(int(x["qtd"] or 0) for x in its)
    d["separadas"] = sum(min(int(x["separado"] or 0), int(x["qtd"] or 0)) for x in its)
    etq = [x for x in its if x.get("identificacao") != "codigo_universal"]
    d["a_etiquetar"] = sum(int(x["qtd"] or 0) for x in etq)                     # etiquetas que a remessa precisa (kit = 1 por kit)
    d["etiquetadas"] = sum(min(int(x.get("etiquetado") or 0), int(x["qtd"] or 0)) for x in etq)
    d["prontas_para_etiquetar"] = sum(max(0, min(int(x["separado"] or 0), int(x["qtd"] or 0)) - int(x.get("etiquetado") or 0)) for x in etq)
    d["validade_minima"] = _limite_validade(d)
    if com_itens:
        d["itens"] = its
        d["etq_esperando"] = int(c.execute("SELECT COALESCE(SUM(qtd),0) AS n FROM full_bips WHERE remessa_id=? AND desfeito=0 AND impresso_em IS NULL",
                                           (rid,)).fetchone()["n"] or 0)
        d["estacao"] = {"nome": _estacao["nome"], "ha_s": int(time.time() - _estacao["em"]) if _estacao["em"] else None}
        d["kits_pendentes"] = sum(1 for x in its if x["comp"] is None)
    return d


def _item_de(c, p):
    """{chave, qtd} (escolhido na tela) ou {inventory_id, sku, titulo, ean, qtd, identificacao, instrucoes, exige_validade}
    (lido do PDF do painel) -> dados do item. Anúncio que não está no cache entra com o que veio do PDF."""
    try:
        q = int(p.get("qtd") or 0)
    except (TypeError, ValueError):
        q = 0
    if q <= 0:
        return None
    a = None
    if p.get("chave"):
        a = c.execute("SELECT * FROM full_anuncios WHERE chave=?", (str(p["chave"]),)).fetchone()
    inv = str(p.get("inventory_id") or "").strip().upper()
    if not a and inv:
        a = c.execute("SELECT * FROM full_anuncios WHERE UPPER(inventory_id)=? ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END LIMIT 1",
                      (inv,)).fetchone()
    if a:
        x = dict(a)
    elif inv:
        x = {"chave": f"INV:{inv}", "item_id": None, "variation_id": None, "inventory_id": inv, "sku": p.get("sku"), "titulo": p.get("titulo"),
             "variacao": None, "foto": None, "ean": p.get("ean")}
    else:
        raise LookupError(f"Anúncio {p.get('chave')} não está na lista do Full (atualize a lista).")
    if p.get("sku") and not x.get("sku"):
        x["sku"] = p["sku"]
    if p.get("ean") and not x.get("ean"):
        x["ean"] = p["ean"]
    x["identificacao"] = p.get("identificacao") or "etiqueta"
    x["instrucoes"] = (str(p.get("instrucoes") or "")[:600]) or None
    x["exige_validade"] = 1 if p.get("exige_validade") else 0
    comp = None
    try:
        import full_auto
        comp = full_auto.kit_do_cache(x.get("sku"))
    except Exception:
        pass
    x["comp"] = None if comp is None else json.dumps(comp, ensure_ascii=False)
    return x, q


def _inserir_item(c, rid, x, q):
    c.execute("INSERT INTO full_itens (remessa_id, chave, item_id, variation_id, inventory_id, sku, titulo, variacao, foto, ean, qtd, "
              "identificacao, instrucoes, exige_validade, comp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, x["chave"], x["item_id"], x["variation_id"], x["inventory_id"], x["sku"], x["titulo"], x["variacao"], x["foto"], x["ean"], q,
               x["identificacao"], x["instrucoes"], x["exige_validade"], x["comp"]))


def _completar_kits(rid):
    """Confere no Bling (segundo plano) se cada produto da remessa é kit; grava a composição nos itens ainda sem bip."""
    try:
        import full_auto
        with db_session() as c:
            its = [dict(r) for r in c.execute("SELECT id, sku FROM full_itens WHERE remessa_id=? AND comp IS NULL", (rid,)).fetchall()]
        for it in its:
            try:
                comp = full_auto.kit_do_sku(it["sku"]) if it["sku"] else []
            except Exception as e:
                print(f"⚠️ full: kit de {it['sku']}: {e}", flush=True)
                continue
            with db_session() as c:
                c.execute("UPDATE full_itens SET comp=? WHERE id=? AND comp IS NULL AND separado=0", (json.dumps(comp, ensure_ascii=False), it["id"]))
    except Exception as e:
        print(f"⚠️ full: conferir kits da remessa {rid}: {e}", flush=True)


def _alvos(itens, k, kd):
    """O que o código lido pode ser: ('un', item) produto simples; ('kit', item) kit inteiro (etiqueta/código do kit);
    ('comp', item, i) uma unidade de um componente do kit."""
    out = []
    eq = lambda a, b: bool(a) and str(a).strip().upper() == b
    for it in itens:
        comp = it["comp"] or []
        if eq(it["sku"], k) or eq(it["inventory_id"], k) or (kd and str(it["ean"] or "").lstrip("0") == kd):
            out.append(("kit" if comp else "un", it, None))
        for i, cp in enumerate(comp):
            if eq(cp.get("sku"), k) or (kd and str(cp.get("ean") or "").lstrip("0") == kd):
                out.append(("comp", it, i))
    return out


def _espaco(tipo, it, i):
    q = int(it["qtd"])
    if tipo == "un":
        return q - int(it["separado"] or 0)
    comp = it["comp"]
    if tipo == "comp":
        cp = comp[i]
        return int(cp["q"]) * q - int(cp.get("bip") or 0)
    return min((int(cp["q"]) * q - int(cp.get("bip") or 0)) // max(1, int(cp["q"])) for cp in comp)   # kits inteiros que cabem


def _conferir_lote(c, rid, rem, it, sku_fis, lote_lido, qtd):
    """Lote ANVISA: escolhe/valida o lote (FEFO) e, se o ML exige validade, só aceita lote que vence depois de
    data de envio + 90 dias. Registra o consumo no FEFO (fefo_bip, ml_id FULL-<remessa>). -> dict ou erro (tipo, msg)."""
    try:
        import fefo_routes as FR
    except Exception:
        return {}
    FR.garantir_tabelas(c)
    key = FR.sku_key(sku_fis)
    lotes = FR.lotes_efetivos(c, key)
    exige = bool(it.get("exige_validade"))
    limite = _limite_validade(rem)
    if not lotes:
        return {"aviso_validade": f"Confira a validade: tem que vencer depois de {limite[8:10]}/{limite[5:7]}/{limite[:4]}." if exige else None}
    bons = [l for l in lotes if not exige or (l["validade"] and l["validade"][:10] >= limite)]
    dt = lambda v: f"{v[8:10]}/{v[5:7]}/{v[:4]}" if v else "sem validade"
    aviso = None
    if lote_lido:
        l = FR.casa_lote(lote_lido, lotes)
        if l is None:
            cands = sorted(FR.candidatos_lote(lote_lido)) or [""]
            r = c.execute("SELECT lote, lote_key, validade FROM fefo_lote WHERE sku_key=? AND lote_key IN (%s) LIMIT 1" % ",".join("?" * len(cands)),
                          (key, *cands)).fetchone()
            if r and exige and (not r["validade"] or r["validade"][:10] < limite):
                return {"erro": ("validade_curta", f"Lote {r['lote']} vence em {dt(r['validade'])}: o Full só aceita validade depois de {dt(limite)}."
                                 + (f" Pegue o lote {bons[0]['lote']} (vence {dt(bons[0]['validade'])})." if bons else ""))}
            l = {"lote": r["lote"] if r else lote_lido, "lote_key": r["lote_key"] if r else FR.lote_key(lote_lido), "validade": r["validade"] if r else None}
            aviso = "Lote fora do estoque do sistema: passou sem conferir a validade." if not r else None
        elif exige and (not l["validade"] or l["validade"][:10] < limite):
            return {"erro": ("validade_curta", f"Lote {l['lote']} vence em {dt(l['validade'])}: o Full só aceita validade depois de {dt(limite)}."
                             + (f" Pegue o lote {bons[0]['lote']} (vence {dt(bons[0]['validade'])})." if bons else " Nenhum lote em estoque serve."))}
        elif bons and l["lote_key"] != bons[0]["lote_key"] and (l["validade"] or "") > (bons[0]["validade"] or ""):
            aviso = f"Tinha lote mais antigo que serve: {bons[0]['lote']} (vence {dt(bons[0]['validade'])})."
    else:
        if not bons:
            return {"erro": ("validade_curta", f"Nenhum lote em estoque vence depois de {dt(limite)} (envio + {VALIDADE_DIAS} dias): esse produto não pode ir para o Full.")}
        l = bons[0]
    c.execute("INSERT INTO fefo_bip (sku_key, lote_key, ml_id, qtd, criado_em) VALUES (?,?,?,?,?)", (key, l["lote_key"], f"FULL-{rid}", qtd, int(time.time())))
    fid = c.execute("SELECT MAX(id) AS m FROM fefo_bip WHERE sku_key=? AND ml_id=?", (key, f"FULL-{rid}")).fetchone()["m"]
    return {"lote": l["lote"], "validade": l.get("validade"), "fefo_id": fid, "aviso": aviso}


def _data_iso(txt):
    """'05/2028', '01/05/2028', '2028-05-01' -> 'AAAA-MM-DD' (mês/ano = último dia do mês)."""
    import calendar
    t = str(txt or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return t
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
    if m:
        d, mo, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = a + 2000 if a < 100 else a
    else:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{2,4})", t)
        if not m:
            return None
        mo, a = int(m.group(1)), int(m.group(2))
        a = a + 2000 if a < 100 else a
        d = calendar.monthrange(a, mo)[1] if 1 <= mo <= 12 else 0
    try:
        from datetime import date
        return date(a, mo, d).isoformat()
    except ValueError:
        return None


def _lote_da_etiqueta(c, it, lote_lido):
    """Lote/validade que vai na etiqueta: o da etiqueta do WMS bipada (SKU*LOTE) ou, na ordem, o lote da unidade
    separada que está sendo etiquetada (n-ésima etiqueta = n-ésima unidade separada). -> (lote, validade)"""
    comp = it["comp"] or []
    fis = comp[0]["sku"] if comp else it["sku"]
    q0 = int(comp[0]["q"]) if comp else 1
    if lote_lido:
        try:
            import fefo_routes as FR
            cands = sorted(FR.candidatos_lote(lote_lido)) or [""]
            r = c.execute("SELECT lote, validade FROM fefo_lote WHERE sku_key=? AND lote_key IN (%s) LIMIT 1" % ",".join("?" * len(cands)),
                          (FR.sku_key(fis), *cands)).fetchone()
            if r:
                return r["lote"], r["validade"]
        except Exception:
            pass
        return lote_lido, None
    alvo = int(it.get("etiquetado") or 0) * q0          # índice da 1ª unidade (do 1º componente) desta etiqueta
    soma = 0
    for b in c.execute("SELECT lote, validade, detalhe, fisico FROM full_bips WHERE item_id=? AND reimpressao=0 AND desfeito=0 ORDER BY id",
                       (it["id"],)).fetchall():
        try:
            det = json.loads(b["detalhe"] or "{}")
        except Exception:
            det = {}
        n = int(det.get("un") or 0) if not comp else int((det.get("comp") or {}).get("0") or 0)
        if n and alvo < soma + n:
            return b["lote"], b["validade"]
        soma += n
    return None, None


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
        return jsonify({"anuncios": rows[:2000], "total": len(rows), "atualizando": st["rodando"], "erro": st["erro"],
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
        frete = re.sub(r"\D", "", str(d.get("frete") or ""))[:20] or None
        with db_session() as c:
            if frete:
                ja = c.execute("SELECT id FROM full_remessas WHERE frete=? AND status<>'cancelada'", (frete,)).fetchone()
                if ja:
                    return jsonify({"error": f"O frete #{frete} já está no WMS (remessa {ja['id']}).", "remessa_id": ja["id"]}), 409
            try:
                itens = [x for x in (_item_de(c, p) for p in d.get("itens") or []) if x]
            except LookupError as e:
                return jsonify({"error": str(e)}), 400
            if not itens:
                return jsonify({"error": "Escolha pelo menos um produto com quantidade."}), 400
            nome = (str(d.get("nome") or "").strip() or (f"Frete #{frete}" if frete else f"Full {time.strftime('%d/%m %H:%M', time.localtime(agora))}"))[:120]
            de = str(d.get("data_envio") or "")[:10] or None
            c.execute("INSERT INTO full_remessas (nome, obs, status, criado_por, criado_em, atualizado_em, frete, data_envio, origem) VALUES (?,?,?,?,?,?,?,?,?)",
                      (nome, (str(d.get("obs") or "")[:500] or None), "aberta", _quem(), agora, agora, frete, de, str(d.get("origem") or "")[:20] or None))
            rid = c.execute("SELECT MAX(id) AS m FROM full_remessas WHERE criado_em=? AND nome=?", (agora, nome)).fetchone()["m"]
            for x, q in itens:
                _inserir_item(c, rid, x, q)
            out = _rem(c, rid)
        if out.get("kits_pendentes"):
            threading.Thread(target=_completar_kits, args=(rid,), daemon=True).start()
        return jsonify({"success": True, "remessa": out})

    @bp.get("/full/remessas/<int:rid>")
    @login_required
    def full_ver(rid):
        _tabelas()
        with db_session() as c:
            d = _rem(c, rid)
        if d and d.get("kits_pendentes") and d["status"] in ABERTAS:
            threading.Thread(target=_completar_kits, args=(rid,), daemon=True).start()
        return jsonify(d) if d else (jsonify({"error": "Remessa não encontrada."}), 404)

    @bp.post("/full/remessas/<int:rid>/dados")
    @login_required
    def full_dados(rid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        with db_session() as c:
            if not c.execute("SELECT 1 FROM full_remessas WHERE id=?", (rid,)).fetchone():
                return jsonify({"error": "Remessa não encontrada."}), 404
            if "data_envio" in d:
                c.execute("UPDATE full_remessas SET data_envio=?, atualizado_em=? WHERE id=?", (str(d["data_envio"] or "")[:10] or None, int(time.time()), rid))
            if str(d.get("nome") or "").strip():
                c.execute("UPDATE full_remessas SET nome=? WHERE id=?", (str(d["nome"]).strip()[:120], rid))
            return jsonify({"success": True, "remessa": _rem(c, rid)})

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
                novos = {x["chave"]: (x, q) for x, q in (y for y in (_item_de(c, p) for p in d.get("itens") or []) if y)}
            except LookupError as e:
                return jsonify({"error": str(e)}), 400
            for it in r["itens"]:
                mexeu = int(it["separado"] or 0) > 0 or any(int(cp.get("bip") or 0) for cp in (it["comp"] or []))
                if it["chave"] in novos:
                    _, q = novos.pop(it["chave"])
                    if q < int(it["separado"] or 0):
                        return jsonify({"error": f"{it['titulo'][:40]}: já separaram {it['separado']}, não dá para pedir menos."}), 409
                    c.execute("UPDATE full_itens SET qtd=? WHERE id=?", (q, it["id"]))
                elif not mexeu:
                    c.execute("DELETE FROM full_itens WHERE id=?", (it["id"],))
                else:
                    return jsonify({"error": f"{it['titulo'][:40]} já começou a ser separado: não dá para tirar."}), 409
            for x, q in novos.values():
                _inserir_item(c, rid, x, q)
            c.execute("UPDATE full_remessas SET atualizado_em=? WHERE id=?", (int(time.time()), rid))
            out = _rem(c, rid)
        if out.get("kits_pendentes"):
            threading.Thread(target=_completar_kits, args=(rid,), daemon=True).start()
        return jsonify({"success": True, "remessa": out})

    @bp.post("/full/remessas/<int:rid>/atualizar")
    @login_required
    def full_atualizar(rid):
        """PDF novo do mesmo frete (Biel 17/11: o painel do ML mudou a remessa e não dá para perder o que já foi feito).
        Troca a lista da remessa pela do PDF sem mexer no que já foi separado/etiquetado. Casa pelo código do Full
        (inventory_id), senão pela chave do anúncio. Pediu menos do que já separaram: a diferença vira sobra (devolver
        à prateleira, botão Devolvi). Produto que saiu do PDF: some se ninguém mexeu; se já mexeram fica com 0 e sobra.
        aplicar=0 só mostra o que muda; aplicar=1 grava (e guarda a lista de antes em full_atualizacoes)."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        aplicar = bool(d.get("aplicar"))
        frete = re.sub(r"\D", "", str(d.get("frete") or ""))[:20] or None
        with db_session() as c:
            r = _rem(c, rid)
            if not r:
                return jsonify({"error": "Remessa não encontrada."}), 404
            if r["status"] in ("enviada", "cancelada"):
                return jsonify({"error": f"Remessa já {'enviada ao Full' if r['status'] == 'enviada' else 'cancelada'}: não dá para atualizar."}), 409
            if frete and r.get("frete") and frete != str(r["frete"]) and not d.get("outro_frete"):
                return jsonify({"error": f"Esse PDF é do Frete #{frete} e esta remessa é do Frete #{r['frete']}. Abra a remessa certa.",
                                "tipo": "outro_frete"}), 409
            try:
                lidos = [y for y in (_item_de(c, p) for p in d.get("itens") or []) if y]
            except LookupError as e:
                return jsonify({"error": str(e)}), 400
            if not lidos:
                return jsonify({"error": "O PDF não trouxe nenhum produto."}), 400
            pdf = {}
            for x, q in lidos:                                  # o mesmo código do Full em duas linhas do PDF: soma
                k = _chave_full(x)
                pdf[k] = (pdf[k][0], pdf[k][1] + q) if k in pdf else (x, q)
            usados, mud = set(), []
            for it in r["itens"]:
                k = _chave_full(it)
                if k not in pdf or k in usados:
                    k = next((kk for kk, (x, _) in pdf.items() if kk not in usados and x.get("chave") == it.get("chave")), k)
                base = {"item_id": it["id"], "titulo": it["titulo"], "codigo": it.get("inventory_id"), "sku": it.get("sku"),
                        "antes": int(it["qtd"] or 0), "separado": int(it["separado"] or 0), "etiquetado": int(it.get("etiquetado") or 0),
                        "kit": bool(it.get("comp"))}
                if k in pdf and k not in usados:
                    usados.add(k)
                    x, q = pdf[k]
                    attrs = (x["identificacao"] != (it.get("identificacao") or "etiqueta") or (x["instrucoes"] or None) != (it.get("instrucoes") or None)
                             or bool(x["exige_validade"]) != bool(it.get("exige_validade")))
                    sep_q = min(_kits_feitos(it["comp"], q) if it.get("comp") else base["separado"], q)
                    mud.append({**base, "tipo": "igual" if q == base["antes"] and not attrs else "mudou", "depois": q,
                                "sobra": _sobra(it, q), "etq_sobra": max(0, base["etiquetado"] - q), "_x": x,
                                "valem": sep_q,
                                "etq_valem": min(base["etiquetado"], q)})
                else:
                    mud.append({**base, "tipo": "saiu", "depois": 0, "acao": "zerar" if _mexeu(it) else "apagar",
                                "sobra": _sobra(it, 0), "etq_sobra": base["etiquetado"]})
            for k, (x, q) in pdf.items():
                if k not in usados:
                    mud.append({"item_id": None, "titulo": x.get("titulo"), "codigo": x.get("inventory_id"), "sku": x.get("sku"), "antes": 0,
                                "depois": q, "separado": 0, "etiquetado": 0, "kit": bool(x.get("comp") and x["comp"] != "[]"),
                                "tipo": "novo", "sobra": 0, "etq_sobra": 0, "_x": x})
            conta = lambda t: sum(1 for m in mud if m["tipo"] == t)
            resumo = {"novos": conta("novo"), "mudaram": conta("mudou"), "sairam": conta("saiu"), "iguais": conta("igual"),
                      "unidades_antes": r["unidades"], "unidades_depois": sum(m["depois"] for m in mud),
                      "separadas": r["separadas"], "etiquetadas": r["etiquetadas"],
                      "sobra": sum(m["sobra"] for m in mud), "etq_sobra": sum(m["etq_sobra"] for m in mud),
                      "separadas_valem": sum(m.get("valem", 0) for m in mud), "etiquetadas_valem": sum(m.get("etq_valem", 0) for m in mud)}
            publico = [{k2: v for k2, v in m.items() if k2 != "_x"} for m in mud]
            if not aplicar:
                return jsonify({"previa": True, "frete": r.get("frete"), "mudancas": publico, "resumo": resumo})
            antes = json.dumps([{k2: v for k2, v in it.items() if k2 not in ("sobra", "etq_sobra")} for it in r["itens"]], ensure_ascii=False, default=str)
            for m in mud:
                if m["tipo"] in ("igual", "mudou"):
                    x, q = m["_x"], m["depois"]
                    it = next(i for i in r["itens"] if i["id"] == m["item_id"])
                    sep = _kits_feitos(it["comp"], q) if it.get("comp") else int(it["separado"] or 0)
                    c.execute("UPDATE full_itens SET qtd=?, separado=?, identificacao=?, instrucoes=?, exige_validade=?, "
                              "sku=COALESCE(sku, ?), ean=COALESCE(ean, ?), foto=COALESCE(foto, ?) WHERE id=?",
                              (q, sep, x["identificacao"], x["instrucoes"], x["exige_validade"], x.get("sku"), x.get("ean"), x.get("foto"), m["item_id"]))
                elif m["tipo"] == "saiu":
                    if m["acao"] == "apagar":
                        c.execute("DELETE FROM full_itens WHERE id=?", (m["item_id"],))
                    else:
                        it = next(i for i in r["itens"] if i["id"] == m["item_id"])
                        c.execute("UPDATE full_itens SET qtd=0, separado=? WHERE id=?", (0 if it.get("comp") else int(it["separado"] or 0), m["item_id"]))
                else:
                    _inserir_item(c, rid, m["_x"], m["depois"])
            agora = int(time.time())
            if str(d.get("data_envio") or "")[:10] and not r.get("data_envio"):
                c.execute("UPDATE full_remessas SET data_envio=? WHERE id=?", (str(d["data_envio"])[:10], rid))
            c.execute("UPDATE full_remessas SET atualizado_em=? WHERE id=?", (agora, rid))
            c.execute("INSERT INTO full_atualizacoes (remessa_id, em, por, tipo, antes, mudancas) VALUES (?,?,?,?,?,?)",
                      (rid, agora, _quem(), "pdf", antes, json.dumps({"resumo": resumo, "mudancas": publico}, ensure_ascii=False, default=str)))
            out = _rem(c, rid)
            if out["status"] == "separada" and (out["separadas"] < out["unidades"] or out["etiquetadas"] < out["a_etiquetar"] or out["sobra"]):
                c.execute("UPDATE full_remessas SET status='separando', fechado_por=NULL, fechado_em=NULL WHERE id=?", (rid,))
                out = _rem(c, rid)
        print(f"[full] remessa {rid} atualizada pelo PDF por {_quem()}: {resumo}", flush=True)
        if out.get("kits_pendentes"):
            threading.Thread(target=_completar_kits, args=(rid,), daemon=True).start()
        return jsonify({"success": True, "mudancas": publico, "resumo": resumo, "remessa": out})

    @bp.post("/full/remessas/<int:rid>/devolver")
    @login_required
    def full_devolver(rid):
        """Sobra devolvida à prateleira: desfaz os últimos bips do produto até caber na quantidade nova (devolve o lote no
        FEFO) e tira as etiquetas a mais (a pessoa arranca a etiqueta da unidade que voltou)."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        try:
            item_id = int(d.get("item_id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        with db_session() as c:
            r = c.execute("SELECT status FROM full_remessas WHERE id=?", (rid,)).fetchone()
            if not r:
                return jsonify({"success": False, "error": "Remessa não encontrada."}), 404
            if r["status"] in ("enviada", "cancelada"):
                return jsonify({"success": False, "error": "Remessa já fechada."}), 409
            row = c.execute("SELECT * FROM full_itens WHERE id=? AND remessa_id=?", (item_id, rid)).fetchone()
            if not row:
                return jsonify({"success": False, "error": "Produto não está nesta remessa."}), 404
            it = dict(row)
            comp = _comp(it)
            it["comp"] = comp
            q = int(it["qtd"] or 0)
            if comp:
                exc = {str(i): max(0, int(cp.get("bip") or 0) - int(cp["q"]) * q) for i, cp in enumerate(comp)}
            else:
                exc = {"un": max(0, int(it["separado"] or 0) - q)}
            exc = {k: v for k, v in exc.items() if v > 0}
            etq_exc = max(0, int(it.get("etiquetado") or 0) - q)
            devolvidas = sum(exc.values())
            for b in c.execute("SELECT * FROM full_bips WHERE remessa_id=? AND item_id=? AND desfeito=0 AND reimpressao=0 ORDER BY id DESC",
                               (rid, item_id)).fetchall():
                if not any(v > 0 for v in exc.values()):
                    break
                b = dict(b)
                try:
                    det = json.loads(b.get("detalhe") or "null") or {"un": int(b["qtd"] or 0)}
                except Exception:
                    det = {"un": int(b["qtd"] or 0)}
                tirado = 0
                if exc.get("un") and int(det.get("un") or 0) > 0:
                    t = min(exc["un"], int(det["un"])); exc["un"] -= t; det["un"] = int(det["un"]) - t; tirado += t
                for j, n in list((det.get("comp") or {}).items()):
                    if exc.get(str(j), 0) > 0 and int(n) > 0:
                        t = min(exc[str(j)], int(n)); exc[str(j)] -= t; det["comp"][j] = int(n) - t; tirado += t
                if not tirado:
                    continue
                resta = int(det.get("un") or 0) + sum(int(v) for v in (det.get("comp") or {}).values())
                if det.get("fefo_id"):
                    if resta <= 0:
                        c.execute("DELETE FROM fefo_bip WHERE id=?", (det["fefo_id"],))
                    else:
                        c.execute("UPDATE fefo_bip SET qtd=CASE WHEN qtd>? THEN qtd-? ELSE 0 END WHERE id=?", (tirado, tirado, det["fefo_id"]))
                if resta <= 0:
                    c.execute("UPDATE full_bips SET desfeito=1 WHERE id=?", (b["id"],))
                else:
                    c.execute("UPDATE full_bips SET detalhe=? WHERE id=?", (json.dumps(det), b["id"]))
            if comp:
                for cp in comp:
                    cp["bip"] = min(int(cp.get("bip") or 0), int(cp["q"]) * q)
                c.execute("UPDATE full_itens SET comp=?, separado=? WHERE id=?", (json.dumps(comp, ensure_ascii=False), _kits_feitos(comp, q), item_id))
            else:
                c.execute("UPDATE full_itens SET separado=? WHERE id=? AND separado>?", (q, item_id, q))
            if etq_exc:
                for e in c.execute("SELECT id FROM full_bips WHERE remessa_id=? AND item_id=? AND desfeito=0 AND reimpressao=2 ORDER BY id DESC LIMIT ?",
                                   (rid, item_id, etq_exc)).fetchall():
                    c.execute("UPDATE full_bips SET desfeito=1 WHERE id=?", (e["id"],))
                c.execute("UPDATE full_itens SET etiquetado=? WHERE id=?", (q, item_id))
            c.execute("INSERT INTO full_atualizacoes (remessa_id, em, por, tipo, antes, mudancas) VALUES (?,?,?,?,?,?)",
                      (rid, int(time.time()), _quem(), "devolver", json.dumps({k2: v for k2, v in it.items()}, ensure_ascii=False, default=str),
                       json.dumps({"item_id": item_id, "devolvidas": devolvidas, "etiquetas": etq_exc})))
            if q == 0:
                c.execute("DELETE FROM full_itens WHERE id=? AND qtd=0 AND separado=0 AND COALESCE(etiquetado,0)=0", (item_id,))
            out = _rem(c, rid)
        return jsonify({"success": True, "devolvidas": devolvidas, "etiquetas": etq_exc, "remessa": out})

    @bp.post("/full/remessas/<int:rid>/bipar")
    @login_required
    def full_bipar(rid):
        """Bip: produto simples conta 1 unidade (= 1 etiqueta); componente de kit conta 1 unidade do componente e, quando
        fecha um kit, sai 1 etiqueta; código do kit inteiro (etiqueta de kit do WMS) conta 1 kit. Lote: da etiqueta do WMS
        (SKU*LOTE*...) ou o FEFO escolhe; validade conferida contra data de envio + 90 dias quando o ML exige."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        bruto = str(d.get("codigo") or "").strip()
        partes = bruto.split("*") if "*" in bruto else [bruto]
        cod = partes[0].strip()
        lote_lido = (partes[1].strip() if len(partes) > 1 else "") or str(d.get("lote") or "").strip()
        try:
            qtd = max(1, int(d.get("quantidade") or 1))
        except (TypeError, ValueError):
            qtd = 1
        aqui = bool(d.get("imprime_aqui"))         # o aparelho que bipou imprime (QZ); senão vai para a fila da estação
        k = cod.upper()
        kd = re.sub(r"\D", "", cod).lstrip("0")
        with db_session() as c:
            r = _rem(c, rid)
            if not r:
                return jsonify({"success": False, "error": "Remessa não encontrada."}), 404
            if r["status"] not in ABERTAS:
                return jsonify({"success": False, "error": "Remessa já fechada."}), 409
            alvos = _alvos(r["itens"], k, kd)
            if not alvos:
                return jsonify({"success": False, "error": f"{cod} não está nesta remessa.", "tipo": "fora"}), 404
            alvos.sort(key=lambda a: (0 if a[0] == "un" else 1, a[1]["id"]))
            alvo = next((a for a in alvos if _espaco(*a) > 0), None)
            if not alvo and all(int(a[1]["qtd"] or 0) == 0 for a in alvos):
                return jsonify({"success": False, "error": f"{alvos[0][1]['titulo'][:50]}: saiu da remessa no PDF novo, não vai para o Full. Devolva à prateleira.",
                                "tipo": "saiu"}), 409
            if not alvo:
                it0 = alvos[0][1]
                meio = next((a for a in alvos if a[0] == "kit" and any(int(cp["q"]) * int(a[1]["qtd"]) > int(cp.get("bip") or 0) for cp in a[1]["comp"])), None)
                if meio:                               # kit montado não cabe: o último kit já começou unidade por unidade
                    falta = ", ".join(f"{int(cp['q']) * int(meio[1]['qtd']) - int(cp.get('bip') or 0)} de {cp['sku']}" for cp in meio[1]["comp"]
                                      if int(cp["q"]) * int(meio[1]["qtd"]) > int(cp.get("bip") or 0))
                    return jsonify({"success": False, "error": f"O último kit já começou unidade por unidade: bipe as unidades soltas que faltam ({falta}).",
                                    "tipo": "kit_meio"}), 409
                return jsonify({"success": False, "error": f"{it0['titulo'][:50]}: já tem tudo o que vai para o Full.", "tipo": "completo"}), 409
            tipo, it, i = alvo
            dar = min(qtd, _espaco(tipo, it, i))
            comp = it["comp"] or []
            if tipo == "un":
                sku_fis, unid = it["sku"], dar
            elif tipo == "comp":
                sku_fis, unid = comp[i]["sku"], dar
            else:
                sku_fis, unid = (comp[0]["sku"], dar * int(comp[0]["q"])) if len(comp) == 1 else (None, 0)
            lt = _conferir_lote(c, rid, r, it, sku_fis, lote_lido, unid) if sku_fis else {}
            if lt.get("erro"):
                t, msg = lt["erro"]
                return jsonify({"success": False, "error": msg, "tipo": t}), 409
            antes = _kits_feitos(comp, it["qtd"]) if comp else int(it["separado"] or 0)
            if tipo == "un":
                novo_sep, etq, det = int(it["separado"] or 0) + dar, dar, {"un": dar}
            else:
                if tipo == "comp":
                    comp[i]["bip"] = int(comp[i].get("bip") or 0) + dar
                    det = {"comp": {str(i): dar}}
                else:
                    for cp in comp:
                        cp["bip"] = int(cp.get("bip") or 0) + dar * int(cp["q"])
                    det = {"comp": {str(j): dar * int(cp["q"]) for j, cp in enumerate(comp)}}
                novo_sep = _kits_feitos(comp, it["qtd"])
                etq = novo_sep - antes
            if lt.get("fefo_id"):
                det["fefo_id"] = lt["fefo_id"]
            sem_etq = (it.get("identificacao") == "codigo_universal")
            n_etq = 0                                  # etiqueta é na 2ª etapa (Etiquetar, na bancada): a separação não imprime
            agora = int(time.time())
            c.execute("UPDATE full_itens SET separado=?, comp=? WHERE id=?", (novo_sep, json.dumps(comp, ensure_ascii=False) if it["comp"] is not None else None, it["id"]))
            pronto = aqui or n_etq <= 0                # nada para imprimir (ou o aparelho imprime): não entra na fila
            c.execute("INSERT INTO full_bips (remessa_id, item_id, qtd, por, em, impresso_em, impresso_por, lote, validade, detalhe, fisico) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, it["id"], n_etq, _quem(), agora, agora if pronto else None,
                       (f"{_quem()} (no aparelho)" if aqui else "sem etiqueta") if pronto else None,
                       lt.get("lote"), lt.get("validade"), json.dumps(det), sku_fis))
            bip_id = c.execute("SELECT MAX(id) AS m FROM full_bips WHERE remessa_id=? AND item_id=? AND em=?", (rid, it["id"], agora)).fetchone()["m"]
            c.execute("UPDATE full_remessas SET status='separando', atualizado_em=? WHERE id=? AND status='aberta'", (agora, rid))
            novo = _rem(c, rid)
        itn = next(x for x in novo["itens"] if x["id"] == it["id"])
        # progresso do que o operador pega fisicamente (SKU do componente ou do produto)
        fis = (sku_fis or it["sku"] or "").upper()
        tot = sep = 0
        for x in novo["itens"]:
            if x["comp"]:
                for cp in x["comp"]:
                    if str(cp["sku"]).upper() == fis:
                        tot += int(cp["q"]) * int(x["qtd"]); sep += int(cp.get("bip") or 0)
            elif str(x["sku"] or "").upper() == fis:
                tot += int(x["qtd"]); sep += int(x["separado"] or 0)
        kit = None
        if comp:
            kit = {"feitos": novo_sep, "total": int(it["qtd"]), "fechou": etq, "por_kit": [{"sku": cp["sku"], "q": int(cp["q"]), "nome": cp.get("nome")} for cp in comp],
                   "falta_no_kit": [{"sku": cp["sku"], "nome": cp.get("nome"), "falta": int(cp["q"]) - (int(cp.get("bip") or 0) - novo_sep * int(cp["q"]))}
                                    for cp in itn["comp"] if novo_sep < int(it["qtd"]) and int(cp["q"]) - (int(cp.get("bip") or 0) - novo_sep * int(cp["q"])) > 0]}
        return jsonify({"success": True, "bip_id": bip_id, "item": itn, "bipado_agora": dar, "tipo": tipo, "etq_esperando": novo.get("etq_esperando", 0),
                        "estacao": novo.get("estacao"), "sku": sku_fis or it["sku"], "nome": (comp[i].get("nome") if tipo == "comp" else None) or it["titulo"],
                        "produto": {"total": tot, "bipado": sep, "falta": tot - sep}, "kit": kit,
                        "lote": lt.get("lote"), "validade": lt.get("validade"), "aviso_lote": lt.get("aviso") or lt.get("aviso_validade"),
                        "remessa": {k2: novo[k2] for k2 in ("unidades", "separadas", "status")},
                        "etiqueta": {"codigo": it["inventory_id"], "titulo": it["titulo"], "variacao": it["variacao"], "qtd": n_etq,
                                     "sem_codigo": not it["inventory_id"] and n_etq > 0, "codigo_universal": sem_etq}})

    @bp.post("/full/remessas/<int:rid>/desfazer")
    @login_required
    def full_desfazer(rid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        with db_session() as c:
            sql, args = "SELECT * FROM full_bips WHERE remessa_id=? AND desfeito=0 AND reimpressao=0", [rid]
            if d.get("item_id"):
                sql += " AND item_id=?"; args.append(int(d["item_id"]))
            b = c.execute(sql + " ORDER BY id DESC LIMIT 1", tuple(args)).fetchone()
            if not b:
                return jsonify({"success": False, "error": "Nada para desfazer."}), 404
            b = dict(b)
            it = dict(c.execute("SELECT * FROM full_itens WHERE id=?", (b["item_id"],)).fetchone())
            try:
                det = json.loads(b.get("detalhe") or "null") or {"un": int(b["qtd"] or 0)}
            except Exception:
                det = {"un": int(b["qtd"] or 0)}
            comp = _comp(it)
            etqd = int(it.get("etiquetado") or 0)
            if det.get("comp") and comp:
                teste = [dict(cp) for cp in comp]
                for j, n in det["comp"].items():
                    teste[int(j)]["bip"] = max(0, int(teste[int(j)].get("bip") or 0) - int(n))
                if _kits_feitos(teste, it["qtd"]) < etqd:
                    return jsonify({"success": False, "error": "Esse kit já foi etiquetado: desfaça a etiqueta primeiro (Etiquetar > desfazer)."}), 409
            elif int(it["separado"] or 0) - int(det.get("un") or 0) < etqd:
                return jsonify({"success": False, "error": "Essa unidade já foi etiquetada: desfaça a etiqueta primeiro (Etiquetar > desfazer)."}), 409
            if det.get("comp") and comp:
                for j, n in det["comp"].items():
                    cp = comp[int(j)]
                    cp["bip"] = max(0, int(cp.get("bip") or 0) - int(n))
                c.execute("UPDATE full_itens SET comp=?, separado=? WHERE id=?", (json.dumps(comp, ensure_ascii=False), _kits_feitos(comp, it["qtd"]), it["id"]))
                unid = sum(int(n) for n in det["comp"].values())
            else:
                unid = int(det.get("un") or 0)
                c.execute("UPDATE full_itens SET separado=CASE WHEN separado>=? THEN separado-? ELSE 0 END WHERE id=?", (unid, unid, it["id"]))
            if det.get("fefo_id"):
                c.execute("DELETE FROM fefo_bip WHERE id=?", (det["fefo_id"],))
            c.execute("UPDATE full_bips SET desfeito=1 WHERE id=?", (b["id"],))
            impressa = b["impresso_em"] is not None and int(b["qtd"] or 0) > 0 and (b.get("impresso_por") or "") != "sem etiqueta"
            return jsonify({"success": True, "desfeito": unid, "etiquetas": int(b["qtd"] or 0), "impressa": impressa, "remessa": _rem(c, rid)})

    @bp.post("/full/remessas/<int:rid>/etiquetar")
    @login_required
    def full_etiquetar(rid):
        """2ª etapa, na bancada: bipa a unidade (ou o kit montado) já separada -> sai 1 etiqueta do Full dela. Não passa do separado."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        partes = str(d.get("codigo") or "").strip().split("*")
        cod = partes[0].strip()
        lote_lido = partes[1].strip() if len(partes) > 1 else ""
        aqui = bool(d.get("imprime_aqui"))
        k, kd = cod.upper(), re.sub(r"\D", "", cod).lstrip("0")
        with db_session() as c:
            r = _rem(c, rid)
            if not r:
                return jsonify({"success": False, "error": "Remessa não encontrada."}), 404
            if r["status"] in ("enviada", "cancelada"):
                return jsonify({"success": False, "error": "Remessa já fechada."}), 409
            alvos = [a for a in _alvos(r["itens"], k, kd)]
            if not alvos:
                return jsonify({"success": False, "error": f"{cod} não está nesta remessa.", "tipo": "fora"}), 404
            its = []
            for _, it, _i in sorted(alvos, key=lambda a: a[1]["id"]):
                if it not in its:
                    its.append(it)
            if all(it.get("identificacao") == "codigo_universal" for it in its):
                return jsonify({"success": False, "error": "Esse produto não leva etiqueta: o Full lê o código de barras dele.", "tipo": "sem_etiqueta"}), 409
            its = [it for it in its if it.get("identificacao") != "codigo_universal"]
            if all(int(x["qtd"] or 0) == 0 for x in its):
                return jsonify({"success": False, "error": f"{its[0]['titulo'][:50]}: saiu da remessa no PDF novo, não leva etiqueta. Devolva à prateleira.",
                                "tipo": "saiu"}), 409
            it = next((x for x in its if int(x.get("etiquetado") or 0) < min(int(x["separado"] or 0), int(x["qtd"]))), None)
            if not it:
                x = its[0]
                if int(x.get("etiquetado") or 0) >= int(x["qtd"]):
                    return jsonify({"success": False, "error": f"{x['titulo'][:50]}: todas as etiquetas já saíram ({x['qtd']}).", "tipo": "completo"}), 409
                return jsonify({"success": False, "error": f"{x['titulo'][:50]}: etiquetou tudo o que foi separado ({x['separado']}). Separe mais antes.",
                                "tipo": "falta_separar"}), 409
            if not it["inventory_id"]:
                return jsonify({"success": False, "error": "Esse anúncio não tem código do Full: a etiqueta não sai. Chame o supervisor."}), 409
            lote, val = _lote_da_etiqueta(c, it, lote_lido)
            if it.get("exige_validade"):              # o ML marcou validade: a etiqueta sai com ela, sempre
                if d.get("validade"):
                    v = _data_iso(d["validade"])
                    if not v:
                        return jsonify({"success": False, "error": "Validade inválida: digite como 05/2028 ou 01/05/2028.", "tipo": "precisa_validade",
                                        "item_id": it["id"]}), 400
                    if v >= _limite_validade(r):          # só guarda validade que serve (as próximas unidades do item usam ela)
                        c.execute("UPDATE full_itens SET validade_etq=? WHERE id=?", (v, it["id"]))
                    val = v if not val else val
                val = val or it.get("validade_etq")
                if not val:
                    return jsonify({"success": False, "error": f"{it['titulo'][:50]}: digite a validade impressa na embalagem.", "tipo": "precisa_validade",
                                    "item_id": it["id"], "titulo": it["titulo"], "minima": _limite_validade(r)}), 409
                if val[:10] < _limite_validade(r):
                    lim = _limite_validade(r)
                    return jsonify({"success": False, "tipo": "validade_curta", "item_id": it["id"],
                                    "error": f"Validade {val[8:10]}/{val[5:7]}/{val[:4]}: o Full só aceita depois de {lim[8:10]}/{lim[5:7]}/{lim[:4]}. Essa unidade não vai."}), 409
            agora = int(time.time())
            c.execute("UPDATE full_itens SET etiquetado=COALESCE(etiquetado,0)+1 WHERE id=?", (it["id"],))
            c.execute("INSERT INTO full_bips (remessa_id, item_id, qtd, por, em, reimpressao, impresso_em, impresso_por, detalhe, lote, validade) "
                      "VALUES (?,?,?,?,?,2,?,?,?,?,?)",
                      (rid, it["id"], 1, _quem(), agora, agora if aqui else None, f"{_quem()} (no aparelho)" if aqui else None, json.dumps({"etq": 1}),
                       lote, val))
            bip_id = c.execute("SELECT MAX(id) AS m FROM full_bips WHERE remessa_id=? AND item_id=? AND em=? AND reimpressao=2", (rid, it["id"], agora)).fetchone()["m"]
            novo = _rem(c, rid)
        itn = next(x for x in novo["itens"] if x["id"] == it["id"])
        return jsonify({"success": True, "bip_id": bip_id, "item": itn, "kit": bool(it["comp"]),
                        "por_kit": [{"sku": cp["sku"], "q": int(cp["q"]), "nome": cp.get("nome")} for cp in (it["comp"] or [])],
                        "remessa": {k2: novo[k2] for k2 in ("unidades", "separadas", "etiquetadas", "a_etiquetar", "prontas_para_etiquetar", "status")},
                        "etq_esperando": novo.get("etq_esperando", 0), "estacao": novo.get("estacao"),
                        "etiqueta": {"codigo": it["inventory_id"], "titulo": it["titulo"], "variacao": it["variacao"], "qtd": 1, "sku": it["sku"],
                                     "validade": val, "lote": lote}})

    @bp.post("/full/remessas/<int:rid>/desetiquetar")
    @login_required
    def full_desetiquetar(rid):
        _tabelas()
        with db_session() as c:
            b = c.execute("SELECT * FROM full_bips WHERE remessa_id=? AND desfeito=0 AND reimpressao=2 ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
            if not b:
                return jsonify({"success": False, "error": "Nenhuma etiqueta para desfazer."}), 404
            c.execute("UPDATE full_bips SET desfeito=1 WHERE id=?", (b["id"],))
            c.execute("UPDATE full_itens SET etiquetado=CASE WHEN COALESCE(etiquetado,0)>0 THEN etiquetado-1 ELSE 0 END WHERE id=?", (b["item_id"],))
            return jsonify({"success": True, "impressa": b["impresso_em"] is not None, "remessa": _rem(c, rid)})

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
            if st == "separada" and (r["separadas"] < r["unidades"] or r["etiquetadas"] < r["a_etiquetar"]):
                return jsonify({"error": f"Ainda falta: separadas {r['separadas']}/{r['unidades']}, etiquetadas {r['etiquetadas']}/{r['a_etiquetar']}."}), 409
            if st == "separada" and r.get("sobra"):
                return jsonify({"error": f"Sobraram {r['sobra']} unidade(s) que o PDF novo tirou: devolva à prateleira e toque em Devolvi antes de fechar."}), 409
            if st == "cancelada" and r["separadas"] > 0:
                return jsonify({"error": "Já tem unidade separada: desfaça os bips antes de cancelar."}), 409
            c.execute("UPDATE full_remessas SET status=?, atualizado_em=?, fechado_por=?, fechado_em=? WHERE id=?",
                      (st, int(time.time()), _quem() if st != "aberta" else None, int(time.time()) if st != "aberta" else None, rid))
            return jsonify({"success": True, "remessa": _rem(c, rid, False)})

    @bp.post("/full/remessas/<int:rid>/reimprimir")
    @login_required
    def full_reimprimir(rid):
        """Etiqueta estragou ou caiu: sai de novo com a mesma validade/lote (não mexe no separado). imprime_aqui: o aparelho
        imprime (devolve a etiqueta pronta); senão vai para a fila da estação."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        try:
            iid, q = int(d.get("item_id")), max(1, min(500, int(d.get("qtd") or 1)))
        except (TypeError, ValueError):
            return jsonify({"error": "item_id e qtd"}), 400
        aqui = bool(d.get("imprime_aqui"))
        with db_session() as c:
            it = c.execute("SELECT * FROM full_itens WHERE id=? AND remessa_id=?", (iid, rid)).fetchone()
            if not it:
                return jsonify({"error": "Produto não está nesta remessa."}), 404
            ult = c.execute("SELECT lote, validade FROM full_bips WHERE item_id=? AND reimpressao IN (1,2) AND desfeito=0 AND validade IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", (iid,)).fetchone() \
                or c.execute("SELECT lote, validade FROM full_bips WHERE item_id=? AND reimpressao=2 AND desfeito=0 ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
            lote, val = (ult["lote"] if ult else None), (ult["validade"] if ult else None) or it["validade_etq"]
            if not val:
                lote2, val2 = _lote_da_etiqueta(c, dict(it, comp=json.loads(it["comp"]) if it["comp"] else None, etiquetado=0), "")
                lote, val = lote or lote2, val2
            if it["exige_validade"] and not val:
                return jsonify({"success": False, "tipo": "precisa_validade", "item_id": iid, "titulo": it["titulo"],
                                "error": f"{str(it['titulo'])[:50]}: o ML exige validade e o sistema não sabe qual é. Bipe a unidade em Etiquetar para digitar a validade."}), 409
            agora = int(time.time())
            c.execute("INSERT INTO full_bips (remessa_id, item_id, qtd, por, em, reimpressao, lote, validade, impresso_em, impresso_por) VALUES (?,?,?,?,?,1,?,?,?,?)",
                      (rid, iid, q, _quem(), agora, lote, val, agora if aqui else None, f"{_quem()} (no aparelho)" if aqui else None))
        return jsonify({"success": True, "etiqueta": {"codigo": it["inventory_id"], "titulo": it["titulo"], "variacao": it["variacao"], "sku": it["sku"],
                                                      "qtd": q, "lote": lote, "validade": val}})

    @bp.post("/full/remessas/<int:rid>/validade")
    @login_required
    def full_validade(rid):
        """Produto que vai SEM etiqueta do Full (o ML lê o código de barras) mas o ML exige validade impressa em cada unidade:
        etiqueta só de validade/lote, uma por unidade separada, com o lote de cada unidade. Corpo: {item_id, validade?}
        (validade digitada da embalagem vale para as unidades sem lote conhecido e fica guardada no item)."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        try:
            iid = int(d.get("item_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "item_id"}), 400
        with db_session() as c:
            r = _rem(c, rid)
            if not r:
                return jsonify({"success": False, "error": "Remessa não encontrada."}), 404
            it = next((x for x in r["itens"] if int(x["id"]) == iid), None)
            if not it:
                return jsonify({"success": False, "error": "Produto não está nesta remessa."}), 404
            lim = _limite_validade(r)
            if d.get("validade"):
                v = _data_iso(d["validade"])
                if not v:
                    return jsonify({"success": False, "tipo": "precisa_validade", "error": "Validade inválida: digite como 05/2028 ou 01/05/2028."}), 400
                if it.get("exige_validade") and v < lim:
                    return jsonify({"success": False, "tipo": "validade_curta",
                                    "error": f"Validade {v[8:10]}/{v[5:7]}/{v[:4]}: o Full só aceita depois de {lim[8:10]}/{lim[5:7]}/{lim[:4]}."}), 409
                c.execute("UPDATE full_itens SET validade_etq=? WHERE id=?", (v, iid))
                it["validade_etq"] = v
            comp = it["comp"] or []
            grupos, sem = {}, 0
            for b in c.execute("SELECT lote, validade, detalhe FROM full_bips WHERE item_id=? AND reimpressao=0 AND desfeito=0 ORDER BY id", (iid,)).fetchall():
                try:
                    det = json.loads(b["detalhe"] or "{}")
                except Exception:
                    det = {}
                n = int(det.get("un") or 0) if not comp else int((det.get("comp") or {}).get("0") or 0) // max(1, int(comp[0]["q"]))
                if n <= 0:
                    continue
                val = b["validade"] or it.get("validade_etq")
                if not val:
                    sem += n
                    continue
                k = (b["lote"] if b["validade"] else None, val[:10])
                grupos[k] = grupos.get(k, 0) + n
            if sem:
                return jsonify({"success": False, "tipo": "precisa_validade", "item_id": iid, "titulo": it["titulo"], "minima": lim, "sem": sem,
                                "error": f"{sem} unidade(s) sem lote conhecido: digite a validade impressa na embalagem."}), 409
            curtas = [v for (_, v) in grupos if it.get("exige_validade") and v < lim]
            if curtas:
                v = min(curtas)
                return jsonify({"success": False, "tipo": "validade_curta",
                                "error": f"Tem unidade com validade {v[8:10]}/{v[5:7]}/{v[:4]}: o Full só aceita depois de {lim[8:10]}/{lim[5:7]}/{lim[:4]}. Troque o lote antes de mandar."}), 409
        etqs = [{"titulo": it["titulo"], "sku": it["sku"], "lote": lt, "validade": v, "qtd": n} for (lt, v), n in sorted(grupos.items(), key=lambda x: x[0][1])]
        return jsonify({"success": True, "etiquetas": etqs, "unidades": sum(e["qtd"] for e in etqs), "separado": int(it["separado"] or 0)})

    @bp.get("/full/etiquetas/fila")
    @login_required
    def full_fila():
        """Computador da Elgin (estação): reserva as etiquetas ainda não impressas e devolve os dados para imprimir."""
        _tabelas()
        est = (str(request.args.get("estacao") or "").strip() or _quem())[:60]
        agora = int(time.time())
        _estacao.update(nome=est, em=agora)
        tk = f"{est}#{uuid.uuid4().hex[:10]}"          # uma reserva por pedido da fila: nunca devolve a mesma etiqueta duas vezes
        with db_session() as c:
            c.execute("UPDATE full_bips SET reservado_por=?, reservado_em=? WHERE impresso_em IS NULL AND desfeito=0 AND em>? "
                      "AND (reservado_em IS NULL OR reservado_em<?) "
                      "AND remessa_id IN (SELECT id FROM full_remessas WHERE status IN ('aberta','separando','separada'))",
                      (tk, agora, agora - 86400, agora - RESERVA_S))
            rows = c.execute("SELECT b.id, b.qtd, b.reimpressao, b.por, b.remessa_id, b.lote, b.validade, i.inventory_id, i.titulo, i.variacao, i.sku, r.nome AS remessa "
                             "FROM full_bips b JOIN full_itens i ON i.id=b.item_id JOIN full_remessas r ON r.id=b.remessa_id "
                             "WHERE b.reservado_por=? AND b.impresso_em IS NULL AND b.desfeito=0 ORDER BY b.id",
                             (tk,)).fetchall()
        return jsonify({"etiquetas": [{"bip_id": r["id"], "codigo": r["inventory_id"], "titulo": r["titulo"], "variacao": r["variacao"], "sku": r["sku"],
                                       "qtd": int(r["qtd"]), "reimpressao": bool(r["reimpressao"]), "por": r["por"], "remessa_id": r["remessa_id"],
                                       "remessa": r["remessa"], "lote": r["lote"], "validade": r["validade"]} for r in rows]})

    @bp.post("/full/etiquetas/impressas")
    @login_required
    def full_impressas():
        _tabelas()
        d = request.get_json(silent=True) or {}
        est = (str(d.get("estacao") or "").strip() or _quem())[:80]
        ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()][:200]
        agora = int(time.time())
        with db_session() as c:
            for i in ids:
                c.execute("UPDATE full_bips SET impresso_em=?, impresso_por=? WHERE id=? AND impresso_em IS NULL", (agora, est, i))
        return jsonify({"success": True, "n": len(ids)})

    try:                                      # sugestão automática + ler o arquivo do painel (full_auto.py)
        import full_auto
        full_auto.registrar(bp)
    except Exception as e:                    # sem o arquivo novo: o resto do Full segue no ar
        print(f"⚠️ full: sugestão/importar não carregou: {e}", flush=True)

    @bp.get("/full/novas")
    @login_required
    def full_novas():
        _tabelas()
        with db_session() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM full_remessas WHERE status IN ('aberta','separando') AND criado_em > ? "
                                              "ORDER BY id DESC LIMIT 10", (int(time.time()) - 3 * 86400,)).fetchall()]
            return jsonify({"remessas": [_rem(c, i, False) for i in ids], "agora": int(time.time())})
