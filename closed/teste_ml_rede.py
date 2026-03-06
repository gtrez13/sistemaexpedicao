import os
import requests
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("ML_ACCESS_TOKEN")
headers = {"Authorization": f"Bearer {token}"}

print("Testando /users/me ...")
r = requests.get("https://api.mercadolibre.com/users/me", headers=headers, timeout=(10, 60))
print("status:", r.status_code)
print("body:", r.text[:300])
