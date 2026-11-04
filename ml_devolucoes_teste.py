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
todas, off = [], 0
while off < 200:
    st, j = get("/post-purchase/v1/claims/search", {"limit": 50, "offset": off, "type": "returns"})
    if st != 200: break
    pg = j.get("data") or []
    todas += pg
    off += 50
    if len(pg) < 50: break
todas.sort(key=lambda c: c.get("date_created") or "", reverse=True)
print("total lido:", len(todas), "| status:", {s_: sum(1 for c in todas if c.get("status") == s_) for s_ in {c.get("status") for c in todas}})
print("--- 8 mais recentes ---")
for c in todas[:8]:
    print(json.dumps({k: c.get(k) for k in ("id", "stage", "status", "reason_id", "resource_id", "date_created")}, ensure_ascii=False))
abertas = [c for c in todas if c.get("status") != "closed"]
alvo = (abertas or todas)[:2]
for c in alvo:
    cid = c["id"]
    print(f"\n--- detalhe claim {cid} ({c.get('status')}) ---")
    st, d = get(f"/post-purchase/v1/claims/{cid}")
    print(json.dumps(d, ensure_ascii=False)[:900])
    st, r = get(f"/post-purchase/v2/claims/{cid}/returns")
    print(json.dumps(r, ensure_ascii=False)[:1500])
