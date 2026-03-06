from services.emitir_via_rpa import emitir_nfe_por_ml_id

emitir_nfe_por_ml_id(
    "2000011707512229",
    headless=False,
    debug_skip_vendas_gerar=True,   # 👈 pula vendas/gerar
)