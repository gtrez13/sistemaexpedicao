#!/usr/bin/env python3
"""SOMENTE LEITURA. Investiga por que o WMS ve 4 devolucoes abertas e o painel do ML mostra 36 'a caminho'.
Olha os pedidos que aparecem no painel e tenta achar a reclamacao/devolucao ligada a eles.
    python ml_devolucoes_teste2.py [PEDIDO ...]
"""
import json, os, sys
import requests
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
tok, uid = os.getenv("ML_ACCESS_TOKEN"), os.getenv("ML_SELLER_ID")
H = {"Authorization": f"Bearer {tok}"}
B = "https://api.mercadolibre.com"
pedidos = sys.argv[1:] or ["2000019239144482", "2000019190821016"]

def get(path, params=None, n=700):
    r = requests.get(B + path, headers=H, params=params, timeout=30)
    print(f"GET {path} {params or ''} -> HTTP {r.status_code}")
    if r.status_code == 401:
        print("token expirado; rode de novo em alguns minutos"); sys.exit(1)
    try:
        j = r.json()
    except ValueError:
        j = {"_raw": r.text[:200]}
    return r.status_code, j

print("conta:", uid)
st, me = get("/users/me")
print("nick:", me.get("nickname"), "| id:", me.get("id"))
for pid in pedidos:
    print("\n=========== pedido", pid)
    st, o = get(f"/orders/{pid}")
    if st == 200:
        print({k: o.get(k) for k in ("status", "tags", "mediations", "pack_id")}, "| shipping:", (o.get("shipping") or {}).get("id"))
        sid = (o.get("shipping") or {}).get("id")
        st2, c = get("/post-purchase/v1/claims/search", {"resource_id": pid})
        print(json.dumps(c, ensure_ascii=False)[:500])
        for m in (o.get("mediations") or []):
            mid = m.get("id")
            get(f"/post-purchase/v1/claims/{mid}")
            _, r = get(f"/post-purchase/v2/claims/{mid}/returns")
            print(json.dumps(r, ensure_ascii=False)[:900])
        if sid:
            _, s = get(f"/shipments/{sid}")
            print({k: s.get(k) for k in ("status", "substatus", "logistic_type", "type", "tags")})
print("\n=========== busca geral sem filtro de tipo (abertas)")
_, j = get("/post-purchase/v1/claims/search", {"status": "opened", "limit": 50})
print("paging:", j.get("paging"))
tipos = {}
for c in (j.get("data") or []):
    tipos[(c.get("type"), c.get("stage"))] = tipos.get((c.get("type"), c.get("stage")), 0) + 1
print("tipos/estagios abertos:", tipos)
