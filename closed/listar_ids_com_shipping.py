from utils.ml_api import MercadoLivreAPI
import json

api = MercadoLivreAPI()

shipping_id = "46443872886"

r_ship = api._request("GET", f"/shipments/{shipping_id}")

print("SHIP STATUS:", r_ship.status_code)

data = r_ship.json()

print("\n===== shipment (completo) =====")
print(json.dumps(data, indent=2, ensure_ascii=False))
