from utils.ml_api import MercadoLivreAPI
import json

api = MercadoLivreAPI()

order_id = "2000011491923277"  # troca por qualquer um

r = api._request("GET", f"/orders/{order_id}")
print("ORDER STATUS:", r.status_code)

order = r.json()

shipping_id = order.get("shipping", {}).get("id")
print("SHIPPING ID:", shipping_id)

if not shipping_id:
    print("❌ Pedido sem shipping.id")
    exit()

r2 = api._request("GET", f"/shipments/{shipping_id}")
print("SHIPMENT STATUS:", r2.status_code)

ship = r2.json()

print("\n===== shipment (resumo) =====")
print("logistic_type:", ship.get("logistic_type"))
print("status:", ship.get("status"))
print("substatus:", ship.get("substatus"))
print("tags:", ship.get("tags"))
print("=============================\n")

# se quiser ver completo:
# print(json.dumps(ship, indent=2, ensure_ascii=False))
