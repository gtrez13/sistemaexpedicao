from utils.ml_api import MercadoLivreAPI

api = MercadoLivreAPI()
dados = api.buscar_pedidos_hoje()

for ped in dados.get("results", []):
    print("ID:", ped.get("id"), "| Status:", ped.get("status"))
