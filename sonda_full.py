"""SÓ LEITURA. O que o Mercado Livre deixa ver de remessas para o Full na conta da LHVMed.
  1) anúncios Full e as etiquetas (tags) deles, procurando "fbm_in_process" (o ML põe quando uma remessa é agendada);
  2) testa as rotas de remessa (inbound) que existem para outros modelos do ML, só para ver se respondem;
  3) recebimentos no Full dos últimos 59 dias (o que já chegou lá).
Uso: venv/bin/python -u sonda_full.py        (não muda nada no ML nem no WMS)"""
import collections
import datetime as dt
import json
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
from services.ml_service import MercadoLivreService

saida = []


def p(s=""):
    print(s)
    saida.append(str(s))


ml = MercadoLivreService()
uid = ml.user_id


def get(path, params=None):
    r = ml._request("GET", path, params=params)
    if r is None:
        return 0, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


# 1) anúncios Full e tags
ids, off = [], 0
while off < 1000:
    st, d = get(f"/users/{uid}/items/search", {"logistic_type": "fulfillment", "limit": 50, "offset": off})
    if st != 200:
        p(f"busca de anúncios Full: HTTP {st} {str(d)[:200]}")
        break
    lote = d.get("results") or []
    ids += lote
    if len(lote) < 50:
        break
    off += 50
p(f"Anúncios no Full: {len(ids)}")
st, d = get(f"/users/{uid}/items/search", {"tags": "fbm_in_process", "limit": 50})
p(f"Busca por tag fbm_in_process: HTTP {st}, total {(d or {}).get('paging', {}).get('total') if isinstance(d, dict) else '-'}")
tag_ids = (d.get("results") or []) if st == 200 and isinstance(d, dict) else []
todos = list(dict.fromkeys(ids + tag_ids))
tags, em_remessa = collections.Counter(), []
for i in range(0, len(todos), 20):
    st, d = get("/items", {"ids": ",".join(todos[i:i + 20]), "attributes": "id,title,status,sub_status,tags,inventory_id,seller_custom_field,available_quantity"})
    for x in (d or []) if st == 200 else []:
        b = x.get("body") or {}
        for t in b.get("tags") or []:
            tags[t] += 1
        if any("fbm" in t or "inbound" in t for t in b.get("tags") or []):
            em_remessa.append(b)
    time.sleep(0.2)
p("\nEtiquetas (tags) que aparecem nos anúncios Full:")
for t, n in tags.most_common(25):
    p(f"   {t:40} {n}")
p(f"\nAnúncios com tag de remessa (fbm/inbound): {len(em_remessa)}")
for b in em_remessa[:30]:
    p(f"   {b.get('id')}  {str(b.get('seller_custom_field') or '-'):14} {b.get('inventory_id') or '-':12} {b.get('status')}/{','.join(b.get('sub_status') or [])}"
      f"  {[t for t in b.get('tags') or [] if 'fbm' in t or 'inbound' in t]}  {str(b.get('title'))[:40]}")

# 2) rotas de remessa de outros modelos (Global Selling / Fully Managed): só o código de resposta
p("\nRotas de remessa (inbound):")
for path, prm in (("/marketplace/fbm/inbounds", {"seller_id": uid}), ("/fbm/inbounds", {"seller_id": uid}),
                  ("/marketplace/fbm/replenishment/orders", {"seller_id": uid}), ("/inbounds/search", {"seller_id": uid})):
    st, d = get(path, prm)
    p(f"   {path:42} HTTP {st}  {json.dumps(d, ensure_ascii=False)[:160] if d else ''}")

# 3) o que chegou no Full nos últimos 59 dias
fim = dt.date.today()
ini = fim - dt.timedelta(days=59)
st, d = get("/stock/fulfillment/operations/search", {"seller_id": uid, "date_from": ini.isoformat(), "date_to": fim.isoformat(), "limit": 1000})
p(f"\nOperações do Full (59 dias): HTTP {st}")
if st == 200 and isinstance(d, dict):
    ops = d.get("results") or []
    por_tipo = collections.Counter(str(o.get("type")) for o in ops)
    p("   " + ", ".join(f"{k}: {v}" for k, v in por_tipo.most_common()))
    rec = collections.defaultdict(int)
    for o in ops:
        if "inbound" in str(o.get("type")).lower():
            rec[str(o.get("date_created") or "")[:10]] += int((o.get("detail") or {}).get("available_quantity") or o.get("quantity") or 0)
    for dia in sorted(rec)[-10:]:
        p(f"   recebido no Full em {dia}: {rec[dia]} un")

arq = f"sonda_full_{time.strftime('%Y%m%d_%H%M')}.txt"
open(arq, "w", encoding="utf-8").write("\n".join(saida) + "\n")
p(f"\nNada foi alterado. Relatório: {arq}")
