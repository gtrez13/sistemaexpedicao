#!/usr/bin/env python3
"""
Testa, SEM mudar o estoque, se o Bling aceita o lançamento de balanço que o "não achado" usa.

Faz um balanço com a MESMA quantidade que o produto já tem no depósito (saldo X -> X) e confere o
saldo depois. Mostra o HTTP de cada chamada (o que interessa: o POST /estoques deve dar 200/201).

Rode na pasta do backend, com o venv:
    python bling_teste_balanco.py SKU            # só LÊ e mostra o que enviaria (não escreve)
    python bling_teste_balanco.py SKU --enviar   # envia o balanço X -> X (estoque não muda)

Use um SKU qualquer que tenha saldo > 0 (ex.: um produto parado). Precisa de BLING_DEPOSITO_ID no .env/ambiente.
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
    sku = sys.argv[1]; enviar = "--enviar" in sys.argv
    dep = os.environ.get("BLING_DEPOSITO_ID", "10488672491").strip()
    bs = BlingService()
    r = bs._get("/produtos", params={"codigo": sku, "limite": 5})
    print("GET /produtos ->", r.status_code)
    prods = [p for p in ((r.json() or {}).get("data") or []) if str(p.get("codigo", "")).lower() == sku.lower()]
    if not prods:
        print("produto não encontrado pelo código"); return 1
    pid = int(prods[0]["id"]); print("produto id:", pid, "-", prods[0].get("nome"))
    s = bs._get("/estoques/saldos", params=[("idsProdutos[]", str(pid))])
    print("GET /estoques/saldos ->", s.status_code)
    saldo = None
    for row in (s.json() or {}).get("data") or []:
        for d in row.get("depositos") or []:
            if str(d.get("id")) == str(dep):
                saldo = float(d.get("saldoFisico") or 0)
    print(f"saldo físico no depósito {dep}:", saldo)
    if saldo is None:
        print("depósito sem saldo para este produto — escolha outro SKU"); return 1
    corpo = {"produto": {"id": pid}, "deposito": {"id": int(dep)}, "operacao": "B", "quantidade": saldo,
             "observacoes": "TESTE WMS: balanço com a mesma quantidade (sem alteração)"}
    print("corpo que seria enviado:", json.dumps(corpo, ensure_ascii=False))
    if not enviar:
        print("\n(nada foi escrito) Para enviar de verdade: acrescente --enviar"); return 0
    w = bs._post("/estoques", json=corpo)
    print("POST /estoques ->", w.status_code, w.text[:300])
    s2 = bs._get("/estoques/saldos", params=[("idsProdutos[]", str(pid))])
    novo = None
    for row in (s2.json() or {}).get("data") or []:
        for d in row.get("depositos") or []:
            if str(d.get("id")) == str(dep):
                novo = float(d.get("saldoFisico") or 0)
    print("saldo depois:", novo, "(igual ao de antes: %s)" % (novo == saldo))
    ok = w.status_code in (200, 201) and novo == saldo
    print("\nRESULTADO:", "OK — o Bling aceita o balanço, pode ligar o zerar." if ok else "FALHOU — me mande esta saída.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
