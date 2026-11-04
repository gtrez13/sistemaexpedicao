#!/usr/bin/env python3
"""SOMENTE LEITURA: mostra o que o Mercado Livre devolve de reclamacoes/devolucoes da conta do WMS.
Serve para eu montar a importacao automatica em cima de dados reais. NAO renova token, NAO escreve nada.
    python ml_devolucoes_teste.py
"""
import json, os, sys
import requests
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

tok, uid = os.getenv("ML_ACCESS_TOKEN"), os.getenv("ML_SELLER_ID")
if not tok:
    print("ML_ACCESS_TOKEN nao encontrado no .env"); sys.exit(1)
H = {"Authorization": f"Bearer {tok}"}
B = "https://api.mercadolibre.com"

def get(path, params=None):
    r = requests.get(B + path, headers=H, params=params, timeout=30)
    print(f"GET {path} {params or ''} -> HTTP {r.status_code}")
    if r.status_code == 401:
        print("token expirado agora (o WMS renova sozinho); rode de novo em alguns minutos"); sys.exit(1)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_raw": r.text[:300]}

print("conta ML:", uid)
st, j = get("/post-purchase/v1/claims/search", {"limit": 10, "type": "returns"})
if st != 200:
    st, j = get("/post-purchase/v1/claims/search", {"limit": 10})
dados = j.get("data") or []
print("paging:", j.get("paging"), "| qtd:", len(dados))
for c in dados[:5]:
    print(json.dumps({k: c.get(k) for k in ("id", "type", "stage", "status", "reason_id", "resource", "resource_id", "date_created")}, ensure_ascii=False))
if dados:
    cid = dados[0].get("id")
    print("\n--- detalhe da primeira ---")
    st, d = get(f"/post-purchase/v1/claims/{cid}")
    print(json.dumps(d, ensure_ascii=False)[:1200])
    st, r = get(f"/post-purchase/v2/claims/{cid}/returns")
    print(json.dumps(r, ensure_ascii=False)[:1200])
