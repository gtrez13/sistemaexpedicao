#!/usr/bin/env python3
"""Só leitura. Mostra o JSON da reclamação e da devolução no ML de uma venda, para achar onde vem o prazo de revisão.
    venv/bin/python sonda_revisao.py 2000019316130802 2000018976768292
"""
import os, sys, json, requests
from dotenv import load_dotenv
load_dotenv(".env"); sys.path.insert(0, os.getcwd())
from services.ml_service import MercadoLivreService
ml = MercadoLivreService()
def g(path, **params):
    r = requests.get("https://api.mercadolibre.com" + path, headers={"Authorization": f"Bearer {ml.access_token}"}, params=params or None, timeout=30)
    try: return r.status_code, r.json()
    except ValueError: return r.status_code, r.text
for venda in sys.argv[1:]:
    st, cl = g("/post-purchase/v1/claims/search", resource="order", resource_id=venda, limit=5)
    for c in (cl.get("data") or []) if isinstance(cl, dict) else []:
        cid = c.get("id")
        print(f"\n===== venda {venda}  claim {cid}  status {c.get('status')} =====")
        s1, j1 = g(f"/post-purchase/v1/claims/{cid}"); print("-- claim v1:", s1); print(json.dumps(j1, ensure_ascii=False, indent=1)[:4000])
        s2, j2 = g(f"/post-purchase/v2/claims/{cid}/returns"); print("-- returns v2:", s2); print(json.dumps(j2, ensure_ascii=False, indent=1)[:4000])
        s3, j3 = g(f"/post-purchase/v1/claims/{cid}/expected-resolutions"); print("-- expected-resolutions:", s3); print(json.dumps(j3, ensure_ascii=False)[:1500])
