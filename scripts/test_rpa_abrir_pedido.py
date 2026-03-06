from services.bling_nfe_rpa import BlingRPA

# só abre o pedido, não emite ainda
BlingRPA(headless=False).emitir("2000011696730329")