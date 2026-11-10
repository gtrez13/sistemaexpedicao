"""Mostra as permissões que o token do ML (LHVMed) tem de verdade + quem é o vendedor da reclamação.
Uso: venv/bin/python -u ml_permissao_teste.py <claim_id>
Renova o token UMA vez (o ML só mostra as permissões na renovação) e grava o novo no .env, igual o WMS faz."""
import sys

import requests
from dotenv import dotenv_values, set_key

ENV = ".env"
API = "https://api.mercadolibre.com"
e = dotenv_values(ENV)
claim = sys.argv[1] if len(sys.argv) > 1 else ""

r = requests.post(f"{API}/oauth/token", data={
    "grant_type": "refresh_token", "client_id": e.get("ML_APP_ID"), "client_secret": e.get("ML_APP_SECRET"),
    "refresh_token": e.get("ML_REFRESH_TOKEN")}, timeout=30)
if r.status_code != 200:
    print("Renovação falhou:", r.status_code, r.text[:300])
    tok = e.get("ML_ACCESS_TOKEN")
else:
    d = r.json()
    tok = d["access_token"]
    set_key(ENV, "ML_ACCESS_TOKEN", tok)
    set_key(ENV, "ML_REFRESH_TOKEN", d.get("refresh_token") or e.get("ML_REFRESH_TOKEN"))
    print("Token renovado e gravado no .env")
    print("Usuário do token:", d.get("user_id"), "| ML_SELLER_ID no .env:", e.get("ML_SELLER_ID"))
    print("Permissões do token:")
    for s in sorted((d.get("scope") or "").split()):
        print("   ", s)

h = {"Authorization": f"Bearer {tok}"}
me = requests.get(f"{API}/users/me", headers=h, timeout=30)
j = me.json() if me.ok else {}
print("users/me:", me.status_code, j.get("id"), j.get("nickname"), "| tags:", ",".join(j.get("tags") or []))

ap = requests.get(f"{API}/applications/{e.get('ML_APP_ID')}", headers=h, timeout=30)
if ap.ok:
    a = ap.json()
    print("Aplicação:", a.get("id"), a.get("name"), "| scopes:", a.get("scopes"), "| dono:", a.get("owner_id"))
else:
    print("Aplicação:", ap.status_code, ap.text[:200])

if claim:
    c = requests.get(f"{API}/post-purchase/v1/claims/{claim}", headers=h, timeout=30)
    print(f"Reclamação {claim}:", c.status_code)
    if c.ok:
        cj = c.json()
        print("   status/stage:", cj.get("status"), cj.get("stage"), "| tipo:", cj.get("type"))
        for p in cj.get("players") or []:
            print("   ", p.get("role"), p.get("type"), p.get("user_id"), "ações:", [x.get("action") for x in p.get("available_actions") or []])
    rt = requests.get(f"{API}/post-purchase/v2/claims/{claim}/returns", headers=h, timeout=30)
    if rt.ok:
        x = rt.json()
        print("   devolução:", x.get("status"), "| status_money:", x.get("status_money"),
              "| destino:", [(s.get("destination") or {}).get("name") for s in x.get("shipments") or []],
              "| envio:", [s.get("status") for s in x.get("shipments") or []])
