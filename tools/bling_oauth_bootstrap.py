import os
import json
import base64
import urllib.parse
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("BLING_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv("BLING_REDIRECT_URI", "http://localhost:8000/callback").strip()

TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"
AUTH_URL  = "https://www.bling.com.br/Api/v3/oauth/authorize"

OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bling_tokens.json")


def basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("❌ Configure BLING_CLIENT_ID e BLING_CLIENT_SECRET no .env")

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    }

    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("\n✅ 1) Abra este link no navegador e autorize:\n")
    print(url)

    print("\n✅ 2) Depois de autorizar, você será redirecionado.")
    print("Pegue o parâmetro ?code=XXXXX da URL e cole aqui.\n")

    code = input("Cole o code aqui: ").strip()
    if not code:
        raise SystemExit("❌ code vazio")

    headers = {
        "Authorization": f"Basic {basic_auth(CLIENT_ID, CLIENT_SECRET)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=(10, 60))
    if r.status_code not in (200, 201):
        raise SystemExit(f"❌ Token exchange falhou: {r.status_code} - {r.text}")

    tokens = r.json() or {}
    # deixa o BlingService calcular expires_at depois, mas já salva tudo
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Tokens salvos em: {OUT_FILE}")
    print("Agora rode seu sistema normal.\n")


if __name__ == "__main__":
    main()

