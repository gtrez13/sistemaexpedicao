from services.ml_service import MercadoLivreService
from services.bling_ui_emitir_nfe import gerar_nfe_no_painel_por_sku_e_ml_id

ml = MercadoLivreService()

# EXEMPLO:
SKU_BIPADO = "SEU_SKU_AQUI"
ML_ID_BIPADO = "2000011696730329"  # pack ou order

print("🚀 Emitindo NF-e via painel (SKU + ML_ID)...")
ok = gerar_nfe_no_painel_por_sku_e_ml_id(
    SKU_BIPADO,
    ML_ID_BIPADO,
    ml_service=ml,
    headless=False,
)
print("RESULT =", ok)