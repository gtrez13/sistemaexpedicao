#!/usr/bin/env python3
"""Só leitura. Por que uma devolução do ML não aparece no WMS?
    venv/bin/python sonda_ml_devolucao.py 2000015865002531
Mostra: qual conta do ML o token enxerga, a venda, as reclamações dela e para onde o produto volta (galpão x armazém Full).
"""
import os, sys, requests
from dotenv import load_dotenv
load_dotenv(".env")
sys.path.insert(0, os.getcwd())
from services.ml_service import MercadoLivreService

venda = sys.argv[1] if len(sys.argv) > 1 else ""
ml = MercadoLivreService()
H = lambda: {"Authorization": f"Bearer {ml.access_token}"}
def g(path, **params):
    r = requests.get("https://api.mercadolibre.com" + path, headers=H(), params=params or None, timeout=30)
    if r.status_code == 401:
        try: ml.renovar_token()
        except Exception as e: print("  (renovar token falhou:", e, ")")
        r = requests.get("https://api.mercadolibre.com" + path, headers=H(), params=params or None, timeout=30)
    try: return r.status_code, r.json()
    except ValueError: return r.status_code, r.text

st, me = g("/users/me")
print(f"== conta do token: HTTP {st}  id {me.get('id') if isinstance(me, dict) else ''}  nick {me.get('nickname') if isinstance(me, dict) else me}")
print(f"   ML_SELLER_ID no .env = {os.environ.get('ML_SELLER_ID', '(vazio)')}")
if not venda:
    sys.exit(0)

st, o = g(f"/orders/{venda}")
if st != 200:
    sp, pk = g(f"/packs/{venda}")
    if sp == 200 and isinstance(pk, dict) and pk.get("orders"):
        venda = str(pk["orders"][0]["id"]); st, o = g(f"/orders/{venda}")
print(f"\n== venda {venda}: HTTP {st}")
if st == 200:
    it = (o.get("order_items") or [{}])[0].get("item") or {}
    print(f"   status {o.get('status')}  seller {(o.get('seller') or {}).get('id')}  item {it.get('id')} '{it.get('title')}'  sku {it.get('seller_sku')}")
    print(f"   logistic_type {((o.get('shipping') or {}).get('logistic_type')) or '?'}  (fulfillment = Full)")
else:
    print("  ", str(o)[:300]); sys.exit(0)

st, cl = g("/post-purchase/v1/claims/search", resource="order", resource_id=venda, limit=20)
print(f"\n== reclamações da venda: HTTP {st}")
for c in (cl.get("data") or []) if isinstance(cl, dict) else []:
    print(f"   claim {c.get('id')}  status {c.get('status')}  stage {c.get('stage')}  type {c.get('type')}  resource {c.get('resource')}  criada {c.get('date_created')}")
    sr, ret = g(f"/post-purchase/v2/claims/{c.get('id')}/returns")
    print(f"   returns: HTTP {sr}")
    if sr == 200 and isinstance(ret, dict):
        for s in ret.get("shipments") or []:
            dest = (s.get("destination") or {})
            print(f"     envio {s.get('id')}  status {s.get('status')}  destino {dest.get('type') or dest}  ")
        print(f"     status_money {ret.get('status_money')}  subtype {ret.get('subtype')}  resolution {ret.get('resolution')}")
    else:
        print("    ", str(ret)[:300])
if isinstance(cl, dict) and not (cl.get("data") or []):
    print("   (nenhuma reclamação nessa venda para esta conta)")
