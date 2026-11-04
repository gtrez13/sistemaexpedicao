#!/usr/bin/env python3
"""SOMENTE LEITURA. Para cada PEDIDO informado: mostra a reclamacao e a devolucao no ML (status, envio de volta, destino)
e se o pedido esta no WMS (e com qual situacao). Serve para descobrir por que uma devolucao do painel do ML nao aparece.
    python ml_devolucoes_teste3.py [PEDIDO ...]
"""
import json, os, sys, time
import requests
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
from db import db_session

PADRAO = ["2000018859306208", "2000018913087838", "2000018903564280", "2000019161894380", "2000019098184430",
          "2000019137046052", "2000019239144482", "2000019076245308", "2000019132782380", "2000018759118626",
          "2000019212218928", "2000019098292160", "2000018873706036", "2000019057467342", "2000019076241992"]
tok = os.getenv("ML_ACCESS_TOKEN")
if not tok:
    sys.exit("ML_ACCESS_TOKEN nao encontrado no .env")
H = {"Authorization": f"Bearer {tok}"}
B = "https://api.mercadolibre.com"


def get(path, params=None):
    for t in range(4):
        time.sleep(0.8)
        r = requests.get(B + path, headers=H, params=params, timeout=30)
        if r.status_code == 401:
            sys.exit("Token do ML vencido agora; rode de novo em alguns minutos.")
        if r.status_code == 429:
            print(f"      (ML pediu calma, espero 30s...)", flush=True); time.sleep(30); continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return 429, None


pedidos = sys.argv[1:] or PADRAO
with db_session() as conn:
    for pid in pedidos:
        rows = conn.execute("SELECT status, ml_claim_id FROM devolucoes WHERE numero_venda=?", (pid,)).fetchall()
        no_wms = ", ".join(f"{r['status']} (claim {r['ml_claim_id']})" for r in rows) or "NAO ESTA NO WMS"
        print(f"\n=== pedido {pid} | WMS: {no_wms}", flush=True)
        code, o = get(f"/orders/{pid}")
        if code != 200:
            print(f"   pedido: HTTP {code}"); continue
        meds = [m.get("id") for m in (o.get("mediations") or [])]
        print(f"   reclamacoes do pedido: {meds or 'nenhuma'}")
        for cid in meds:
            c1, cl = get(f"/post-purchase/v1/claims/{cid}")
            if c1 == 200:
                print(f"   claim {cid}: status={cl.get('status')} stage={cl.get('stage')} type={cl.get('type')} motivo={cl.get('reason_id')} criada={str(cl.get('date_created'))[:10]}")
            else:
                print(f"   claim {cid}: detalhe HTTP {c1}")
            c2, ret = get(f"/post-purchase/v2/claims/{cid}/returns")
            if c2 == 200:
                shs = [(s.get("type"), s.get("status"), (s.get("destination") or {}).get("name")) for s in (ret.get("shipments") or [])]
                print(f"      devolucao: status={ret.get('status')} refund_at={ret.get('refund_at')} envios(tipo,status,destino)={shs}")
            else:
                print(f"      devolucao: HTTP {c2} {(ret or {}).get('message', '')}")
