from services.bling_nfe_rpa import BlingRPA

if __name__ == "__main__":
    # troca pra uma NF que você sabe que existe
    nf = "10886"

    rpa = BlingRPA(headless=False)
    rpa.imprimir_combo_por_numero_nf(nf)