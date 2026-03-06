from functools import lru_cache
from services.bling_estrutura_robo import descobrir_estrutura

class EstruturaService:

    # cache evita abrir navegador toda hora
    @lru_cache(maxsize=300)
    def obter_estrutura(self, sku: str):
        try:
            estrutura = descobrir_estrutura(sku)
            return estrutura or []
        except Exception as e:
            print("Erro ao obter estrutura:", e)
            return []