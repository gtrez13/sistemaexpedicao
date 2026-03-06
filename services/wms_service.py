from collections import defaultdict


class WMSIndex:
    """
    Motor de separação por SKU (WMS).

    Fluxo esperado:
        1) Tela 2 envia SKU bipado
        2) processar_bip() decide pedido
        3) Se completar → acao="IMPRIMIR"
        4) Se faltar item → acao="MOSTRAR"
    """

    def __init__(self, pedidos: list[dict]):
        self.orders = {}
        self.sku_index = defaultdict(list)

        for order in pedidos:
            oid = str(order["id"])

            self.orders[oid] = {
                "order": order,              # guarda pedido original
                "faltantes": {},
                "completo": False
            }

            falt = self.orders[oid]["faltantes"]

            # monta mapa de itens faltantes (somando quantidades)
            for item in order.get("order_items", []):
                sku = str(item.get("seller_sku", "")).strip()
                qtd = int(item.get("quantity", 1))

                if not sku or qtd <= 0:
                    continue

                falt[sku] = falt.get(sku, 0) + qtd

            # indexa sku → oid (1 vez por sku)
            for sku in falt.keys():
                self.sku_index[sku].append(oid)

    # =========================================================
    # Decide pedido pelo primeiro bip
    # =========================================================
    def decidir_pedido(self, sku: str):
        sku = str(sku).strip()
        candidatos = self.sku_index.get(sku, [])

        # remove pedidos completos
        candidatos = [
            oid for oid in candidatos
            if oid in self.orders and not self.orders[oid]["completo"]
        ]

        if not candidatos:
            return None, "SKU_NAO_EXISTE"

        if len(candidatos) == 1:
            return candidatos[0], "AUTO"

        # heurística: pedido com menos itens faltantes
        melhor = None
        menor = 10**9

        for oid in candidatos:
            total = sum(self.orders[oid]["faltantes"].values())
            if total < menor:
                menor = total
                melhor = oid

        return melhor, "HEURISTICA"

    # =========================================================
    # Bip simples
    # =========================================================
    def bipar(self, oid: str, sku: str):
        oid = str(oid)
        sku = str(sku).strip()

        if oid not in self.orders:
            return "PEDIDO_INVALIDO"

        pedido = self.orders[oid]

        if pedido["completo"]:
            return "PEDIDO_JA_COMPLETO"

        if sku not in pedido["faltantes"]:
            return "SKU_ERRADO"

        pedido["faltantes"][sku] -= 1

        if pedido["faltantes"][sku] <= 0:
            del pedido["faltantes"][sku]

        if not pedido["faltantes"]:
            pedido["completo"] = True
            return "PEDIDO_COMPLETO"

        return "OK"

    # =========================================================
    # Bip inteligente (decide ou troca pedido)
    # =========================================================
    def bipar_auto(self, oid_atual: str | None, sku: str):
        sku = str(sku).strip()
        oid_atual = str(oid_atual).strip() if oid_atual else None

        # se não tem pedido atual → decide
        if not oid_atual:
            novo_oid, motivo = self.decidir_pedido(sku)
            if not novo_oid:
                return None, "SKU_NAO_EXISTE", "SKU_NAO_EXISTE"

            status = self.bipar(novo_oid, sku)
            return novo_oid, status, f"DECIDIU:{motivo}"

        # tenta no pedido atual
        status = self.bipar(oid_atual, sku)
        if status != "SKU_ERRADO":
            return oid_atual, status, "MANTEVE"

        # tenta outro pedido que tenha esse SKU
        novo_oid, motivo = self.decidir_pedido(sku)
        if not novo_oid:
            return oid_atual, "SKU_ERRADO", "SEM_CANDIDATO_VALIDO"

        status2 = self.bipar(novo_oid, sku)
        return novo_oid, status2, f"TROCOU_POR_SKU:{motivo}"

    # =========================================================
    # Retorna faltantes
    # =========================================================
    def resumo_faltantes(self, oid: str):
        p = self.orders.get(str(oid))
        if not p:
            return {}
        return dict(p.get("faltantes") or {})

    # =========================================================
    # FUNÇÃO PRINCIPAL (Tela 2 chama isso)
    # =========================================================
    def processar_bip(self, sku: str, oid_atual: str | None = None):
        """
        Retorno padrão para frontend:
        {
            ok: bool,
            oid: str | None,
            status: str,
            motivo: str,
            acao: "IMPRIMIR" | "MOSTRAR" | "ERRO",
            faltantes: dict,
            msg: str
        }
        """

        oid_usado, status, motivo = self.bipar_auto(oid_atual, sku)

        if oid_usado is None:
            return {
                "ok": False,
                "oid": None,
                "status": status,
                "motivo": motivo,
                "acao": "ERRO",
                "faltantes": {},
                "msg": "SKU não encontrado em nenhum pedido."
            }

        if status == "PEDIDO_COMPLETO":
            return {
                "ok": True,
                "oid": str(oid_usado),
                "status": status,
                "motivo": motivo,
                "acao": "IMPRIMIR",
                "faltantes": {},
                "msg": f"Pedido {oid_usado} completo. Imprimir DANFE + etiqueta."
            }

        return {
            "ok": True,
            "oid": str(oid_usado),
            "status": status,
            "motivo": motivo,
            "acao": "MOSTRAR",
            "faltantes": self.resumo_faltantes(oid_usado),
            "msg": f"Pedido {oid_usado} em separação."
        }