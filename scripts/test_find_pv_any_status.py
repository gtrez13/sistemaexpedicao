from services.bling_service import BlingService
from services.ml_service import MercadoLivreService

b = BlingService()
ml = MercadoLivreService()

ml_id = "2000011696730329"  # pode ser order ou pack

pv_id = b.resolver_pv_id_por_ml_id(ml, ml_id, dias=15)
print("PV_ID =", pv_id)