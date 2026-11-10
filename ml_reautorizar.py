"""Pega um token do ML com as permissões novas da aplicação (ex.: Comunicação pós-venda com escrita).

1) venv/bin/python -u ml_reautorizar.py
   -> renova o token; se a permissão nova já veio, grava e pronto.
   -> se não veio, mostra o LINK de autorização (precisa da URI de redirect da aplicação:
      passe como 1º argumento ou deixe ML_REDIRECT_URI no .env).
2) Abra o link logado na conta MSDMED, autorize, copie o endereço inteiro da página para onde o ML mandar
   (tem ?code=TG-...) e rode:
   venv/bin/python -u ml_reautorizar.py URI_DE_REDIRECT 'ENDERECO_COPIADO'
Só grava se o token for da MESMA conta do .env (ML_SELLER_ID). Backup do .env antes de gravar."""
import base64
import hashlib
import os
import secrets
import shutil
import sys
import time
from urllib.parse import parse_qs, quote, urlparse

import requests
from dotenv import dotenv_values, set_key

ENV = ".env"
API = "https://api.mercadolibre.com"
PKCE = ".ml_pkce_verifier"
QUER = "urn:ml:mktp:comunication:/read-write"

e = dotenv_values(ENV)
app_id, secret, seller = e.get("ML_APP_ID"), e.get("ML_APP_SECRET"), str(e.get("ML_SELLER_ID") or "")
redirect = (sys.argv[1] if len(sys.argv) > 1 else "") or e.get("ML_REDIRECT_URI") or e.get("ML_REDIRECT_URL") or ""
retorno = sys.argv[2] if len(sys.argv) > 2 else ""


def gravar(d):
    if str(d.get("user_id")) != seller:
        print(f"PAREI: o token é da conta {d.get('user_id')}, mas o .env é da {seller}. Autorize logado na conta certa (MSDMED). Nada foi gravado.")
        sys.exit(1)
    bk = f".env.bak_mltoken_{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy(ENV, bk)
    set_key(ENV, "ML_ACCESS_TOKEN", d["access_token"])
    if d.get("refresh_token"):
        set_key(ENV, "ML_REFRESH_TOKEN", d["refresh_token"])
    if redirect and not e.get("ML_REDIRECT_URI"):
        set_key(ENV, "ML_REDIRECT_URI", redirect)
    print(f"Token gravado no .env (backup: {bk}).")


def mostrar(d):
    sc = (d.get("scope") or "").split()
    print("Pós-venda:", "LEITURA E ESCRITA" if QUER in sc else "só leitura" if "urn:ml:mktp:comunication:/read-only" in sc else "sem acesso")
    return QUER in sc


if retorno:
    code = parse_qs(urlparse(retorno).query).get("code", [retorno.strip()])[0]
    data = {"grant_type": "authorization_code", "client_id": app_id, "client_secret": secret, "code": code, "redirect_uri": redirect}
    if os.path.exists(PKCE):
        data["code_verifier"] = open(PKCE).read().strip()
    r = requests.post(f"{API}/oauth/token", data=data, timeout=30)
    if r.status_code != 200:
        print("O ML recusou a troca do código:", r.status_code, r.text[:400])
        print("(o código TG- vale poucos minutos e só 1 vez; gere o link de novo e repita)")
        sys.exit(1)
    d = r.json()
    gravar(d)
    ok = mostrar(d)
    if os.path.exists(PKCE):
        os.remove(PKCE)
    print("PRONTO: reinicie o WMS." if ok else "Autorizou, mas a permissão ainda veio só leitura: confira se salvou 'Leitura e escrita' na aplicação.")
    sys.exit(0)

# passo 1: renovar e ver se a permissão já veio
r = requests.post(f"{API}/oauth/token", data={"grant_type": "refresh_token", "client_id": app_id, "client_secret": secret,
                                               "refresh_token": e.get("ML_REFRESH_TOKEN")}, timeout=30)
if r.status_code == 200:
    d = r.json()
    gravar(d)
    if mostrar(d):
        print("PRONTO: a permissão já veio na renovação. Reinicie o WMS e teste o 'Avisar o ML'.")
        sys.exit(0)
else:
    print("Renovação falhou:", r.status_code, r.text[:200])

if not redirect:
    print("\nFalta a URI de redirect da aplicação (DevCenter > aplicação > 'URI de redirect'). Rode de novo passando ela:")
    print("   venv/bin/python -u ml_reautorizar.py 'https://SUA-URI-DE-REDIRECT'")
    sys.exit(1)
ver = secrets.token_urlsafe(64)[:96]
open(PKCE, "w").write(ver)
os.chmod(PKCE, 0o600)
ch = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
link = (f"https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={app_id}"
        f"&redirect_uri={quote(redirect, safe='')}&code_challenge={ch}&code_challenge_method=S256")
print("\nAbra este link logado na conta MSDMED e clique em Autorizar:\n")
print(link)
print("\nDepois copie o endereço inteiro da página que abrir (tem ?code=TG-...) e rode:")
print(f"   venv/bin/python -u ml_reautorizar.py '{redirect}' 'ENDERECO_COPIADO' && sudo systemctl restart wms-russia.service")
