from services.bling_nfe_rpa import BlingRPA

rpa = BlingRPA(headless=False)
rpa.debug_combo_baixar_sem_imprimir("002011")