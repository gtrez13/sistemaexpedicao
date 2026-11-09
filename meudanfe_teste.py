#!/usr/bin/env python3
"""TESTE Meu Danfe (mostra as respostas cruas). Custa R$0,03 por chave nova.
    venv/bin/python meudanfe_teste.py <chave44>
"""
import os, re, sys, time, json, requests
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
TOK = os.environ.get("MEUDANFE_TOKEN"); BASE = "https://api.meudanfe.com.br/v2"
ch = re.sub(r"\D", "", sys.argv[1] if len(sys.argv) > 1 else "")
assert TOK and len(ch) == 44, "precisa MEUDANFE_TOKEN no .env e uma chave de 44 dígitos"
H = {"Api-Key": TOK}
def show(r):
    print(f"   HTTP {r.status_code} {r.headers.get('content-type')}")
    t = r.text
    try:
        j = r.json()
        for k in ("data", "xml", "base64"):
            if isinstance(j, dict) and isinstance(j.get(k), str) and len(j[k]) > 200:
                j[k] = j[k][:120] + f"... ({len(j[k])} chars)"
        print("  ", json.dumps(j, ensure_ascii=False)[:800])
    except Exception:
        print("  ", t[:400])
print(">> GET xml (grátis se já estiver na sua área)"); show(requests.get(f"{BASE}/fd/get/xml/{ch}", headers=H, timeout=30))
print(">> PUT add (busca na SEFAZ — R$0,03)");          r = requests.put(f"{BASE}/fd/add/{ch}", headers=H, timeout=60); show(r)
for i in range(6):
    time.sleep(3)
    print(f">> GET xml tentativa {i+1}"); r = requests.get(f"{BASE}/fd/get/xml/{ch}", headers=H, timeout=30); show(r)
    if r.status_code == 200 and "infNFe" in (r.text or "") or "PE5GZ" in (r.text or ""):
        break
