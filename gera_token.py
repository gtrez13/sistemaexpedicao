import requests
import base64
import json

CLIENT_ID = "1a6535ec366a7d4e0225ba871f62da24a4887f3d"
CLIENT_SECRET = "4eaeca0637decb720d784a1aaf7d5ae82acfa60765afd34b2facf5153a6b"
CODE = "b42ef2bde4ac67bb11405d5b24862b44168d5abf"
REDIRECT_URI = "http://127.0.0.1:5000/auth/callback"

auth = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

headers = {
    "Authorization": f"Basic {auth}",
    "Content-Type": "application/x-www-form-urlencoded"
}

data = {
    "grant_type": "authorization_code",
    "code": CODE,
    "redirect_uri": REDIRECT_URI
}

r = requests.post(
    "https://api.bling.com.br/Api/v3/oauth/token",
    headers=headers,
    data=data
)

tokens = r.json()
print(tokens)

with open("bling_tokens.json", "w") as f:
    json.dump(tokens, f, indent=2)

print("✅ tokens salvos em bling_tokens.json")