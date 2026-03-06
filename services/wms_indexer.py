from collections import defaultdict


class WMSIndex:
    def __init__(self, pedidos):
        self.orders = {}
        self.sku_index = defaultdict(list)

        for order in pedidos:
            oid = str(order["id"])

            self.orders[oid] = {
                "order": order,
                "faltantes": {},
                "completos": False
            }

            falt = self.orders[oid]["faltantes"]

            # monta mapa de itens faltantes (SOMANDO)
            for item in order.get("order_items", []):
                sku = str(item.get("seller_sku")).strip()
                qtd = int(item.get("quantity", 1))

                if not sku or qtd <= 0:
                    continue

                falt[sku] = int(falt.get(sku, 0)) + qtd

            # indexa SKU -> oid (1x por sku)
            for sku in falt.keys():
                self.sku_index[sku].append(oid)

    def decidir_pedido(self, sku):
        sku = str(sku).strip()
        candidatos = self.sku_index.get(sku, [])

        # remove duplicados + ignora pedidos completos
        candidatos = [oid for oid in dict.fromkeys(candidatos) if oid in self.orders and not self.orders[oid]["completos"]]

        if not candidatos:
            return None, "SKU_NAO_EXISTE"

        if len(candidatos) == 1:
            return candidatos[0], "AUTO"

        melhor = None
        menor_itens = 10**9

        for oid in candidatos:
            faltantes = sum(self.orders[oid]["faltantes"].values())
            if faltantes < menor_itens:
                menor_itens = faltantes
                melhor = oid

        return melhor, "HEURISTICA"

    def bipar(self, oid, sku):
        oid = str(oid)
        sku = str(sku).strip()

        if oid not in self.orders:
            return "PEDIDO_INVALIDO"

        pedido = self.orders[oid]

        if pedido.get("completos"):
            return "PEDIDO_JA_COMPLETO"

        if sku not in pedido["faltantes"]:
            return "SKU_ERRADO"

        pedido["faltantes"][sku] -= 1

        if pedido["faltantes"][sku] <= 0:
            del pedido["faltantes"][sku]

        if not pedido["faltantes"]:
            pedido["completos"] = True
            return "PEDIDO_COMPLETO"

        return "OK"

    # ✅ ADICIONA ISSO (faltava)
    def bipar_auto(self, oid_atual, sku):
        sku = str(sku).strip()
        oid_atual = str(oid_atual).strip() if oid_atual else None

        if not oid_atual:
            novo_oid, motivo = self.decidir_pedido(sku)
            if not novo_oid:
                return None, "SKU_NAO_EXISTE", "SKU_NAO_EXISTE"
            return novo_oid, self.bipar(novo_oid, sku), f"DECIDIU:{motivo}"

        status = self.bipar(oid_atual, sku)
        if status != "SKU_ERRADO":
            return oid_atual, status, "MANTEVE"

        novo_oid, motivo = self.decidir_pedido(sku)
        if not novo_oid:
            return oid_atual, "SKU_ERRADO", "SEM_CANDIDATO_VALIDO"

        return novo_oid, self.bipar(novo_oid, sku), f"TROCOU_POR_SKU:{motivo}"

    def resumo_faltantes(self, oid: str):
        p = self.orders.get(str(oid))
        if not p:
            return {}
        return dict(p.get("faltantes") or {})

    def processar_bip(self, sku: str, oid_atual: str | None = None):
        oid_usado, status, motivo = self.bipar_auto(oid_atual, sku)

        if oid_usado is None:
            return {
                "ok": False,
                "oid": None,
                "status": status,
                "motivo": motivo,
                "acao": "ERRO",
                "faltantes": {},
                "msg": "SKU não encontrado em nenhum pedido.",
            }

        if status == "PEDIDO_COMPLETO":
            return {
                "ok": True,
                "oid": str(oid_usado),
                "status": status,
                "motivo": motivo,
                "acao": "IMPRIMIR",
                "faltantes": {},
                "msg": f"Pedido {oid_usado} completo. Imprimir DANFE + etiqueta.",
            }

        return {
            "ok": True,
            "oid": str(oid_usado),
            "status": status,
            "motivo": motivo,
            "acao": "MOSTRAR",
            "faltantes": self.resumo_faltantes(oid_usado),
            "msg": f"Pedido {oid_usado} em separação. Ainda faltam itens.",
        }

    def bipar_quantidade(self, oid, sku, quantidade):
        oid = str(oid)
        sku = str(sku).strip()
        quantidade = int(quantidade)

        if oid not in self.orders:
            return "PEDIDO_INVALIDO"

        pedido = self.orders[oid]

        if sku not in pedido["faltantes"]:
            return "SKU_ERRADO"

        pedido["faltantes"][sku] -= quantidade

        if pedido["faltantes"][sku] <= 0:
            del pedido["faltantes"][sku]

        if not pedido["faltantes"]:
            pedido["completos"] = True
            return "PEDIDO_COMPLETO"

        return "OK"