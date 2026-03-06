import os, requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("ML_ACCESS_TOKEN")
shipping_id = "46450306712"  # troca por um shipping.id do teu print

headers = {"Authorization": f"Bearer {token}"}

url = f"https://api.mercadolibre.com/shipments/{shipping_id}"
r = requests.get(url, headers=headers, timeout=(10, 60))

print("status:", r.status_code)
print(r.text[:1200])
