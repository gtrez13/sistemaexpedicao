#!/usr/bin/env python3
"""Mostra TODOS os saldos que o Bling devolve para um SKU (só LÊ): por depósito, físico e virtual,
e como o WMS soma. Serve para entender divergência entre o estoque do WMS e o da tela do Bling.
    python bling_saldos.py SKU [SKU2 ...]
"""
import json
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
from services.bling_service import BlingService


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 2
    bs = BlingService()
    nomes = {}
    r = bs._get("/depositos", params={"limite": 100})
    if r.status_code == 200:
        for d in (r.json() or {}).get("data") or []:
            nomes[str(d.get("id"))] = f'{d.get("descricao")}{" (padrao)" if d.get("padrao") else ""}'
    for sku in sys.argv[1:]:
        print("\n=====", sku)
        r = bs._get("/produtos", params={"codigo": sku, "limite": 5})
        prods = [p for p in ((r.json() or {}).get("data") or []) if str(p.get("codigo", "")).lower() == sku.lower()]
        if not prods:
            print("produto não encontrado"); continue
        p = prods[0]; pid = int(p["id"])
        print("id:", pid, "| nome:", p.get("nome"), "| formato:", p.get("formato"), "| tipo:", p.get("tipo"))
        print("estoque na lista de produtos (campo estoque):", json.dumps(p.get("estoque"), ensure_ascii=False))
        s = bs._get("/estoques/saldos", params=[("idsProdutos[]", str(pid))])
        print("GET /estoques/saldos ->", s.status_code)
        soma = 0.0
        for row in (s.json() or {}).get("data") or []:
            print("  totais: saldoFisicoTotal=%s saldoVirtualTotal=%s" % (row.get("saldoFisicoTotal"), row.get("saldoVirtualTotal")))
            for d in row.get("depositos") or []:
                print("  depósito %s %-28s físico=%s virtual=%s" % (d.get("id"), nomes.get(str(d.get("id")), "?"), d.get("saldoFisico"), d.get("saldoVirtual")))
                soma += float(d.get("saldoFisico") or 0)
        print("  => o WMS mostra a SOMA do saldo físico de todos os depósitos:", soma)


if __name__ == "__main__":
    sys.exit(main())
