from services.estrutura_service import EstruturaService


class PickingResolver:

    def __init__(self):
        self.estrutura = EstruturaService()

    # -------------------------------------------------
    # Detecta se o produto PARECE ser um KIT
    # -------------------------------------------------
    def _parece_kit(self, nome: str, sku: str) -> bool:
        if not nome:
            return False

        texto = f"{nome} {sku}".lower()

        palavras_kit = [
            "kit",
            "conjunto",
            "combo",
            "pack",
            "c/ ",
            " c/",
            "+",
            " com ",
            " - ",
        ]

        return any(p in texto for p in palavras_kit)

    # -------------------------------------------------
    # Resolve item do pedido (explode kit se necessário)
    # -------------------------------------------------
    def resolver_item(self, sku, nome, quantidade):

        # 🔒 FILTRO ANTES DO ROBÔ
        if not self._parece_kit(nome, sku):
            return [{
                "sku": sku,
                "nome": nome,
                "qtd": quantidade,
                "parent_sku": None,
                "parent_nome": None
            }]

        # 🔥 Só chega aqui se parecer kit
        estrutura = self.estrutura.obter_estrutura(sku)

        # não é kit de verdade
        if not estrutura:
            return [{
                "sku": sku,
                "nome": nome,
                "qtd": quantidade,
                "parent_sku": None,
                "parent_nome": None
            }]

        # explode componentes
        saida = []
        for comp in estrutura:
            saida.append({
                "sku": comp["sku"],
                "nome": comp["nome"],
                "qtd": quantidade * comp["quantidade"],
                "parent_sku": sku,
                "parent_nome": nome
            })

        return saida