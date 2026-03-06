from services.bling_nfe_rpa import BlingRPA

rpa = BlingRPA(headless=False)

# usa o numero da NF da tela
rpa.enviar_nfe_existente("010172")