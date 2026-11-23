#!/usr/bin/env python3
"""Só leitura. Por que o WMS não acha um pedido de compra que está em aberto no Bling? (Biel 23/11, NF 70644 DORJA x pedido 3304)
    venv/bin/python sonda_pc.py --numero 3304 --cnpj 00.000.000/0001-00
Mostra: o pedido pelo número (fornecedor, situação), os cadastros do Bling com esse CNPJ e os pedidos em aberto de cada um.
"""
import argparse, os, re, sys, time
from dotenv import load_dotenv
load_dotenv(".env")
sys.path.insert(0, os.getcwd())
from services.bling_service import BlingService

dig = lambda s: re.sub(r"\D", "", str(s or ""))
ap = argparse.ArgumentParser(); ap.add_argument("--numero", required=True); ap.add_argument("--cnpj", required=True)
a = ap.parse_args()
b = BlingService()
g = lambda p, **q: (b._get_json(p, q or None) or {}).get("data") or []

print(f"== pedido de compra {a.numero} pelo número")
pcs = g("/pedidos/compras", numero=a.numero, limite=10)
if not pcs:
    pcs = [p for p in g("/pedidos/compras", limite=100) if str(p.get("numero")) == str(a.numero)]
for p in pcs:
    det = (b._get_json(f"/pedidos/compras/{p['id']}") or {}).get("data") or {}
    f = det.get("fornecedor") or p.get("fornecedor") or {}
    print(f"  id {p['id']}  nº {p.get('numero')}  data {p.get('data')}  total {p.get('total')}  situação {(p.get('situacao') or {}).get('valor')} "
          f"(0 aberto,1 atendido,2 cancelado,3 andamento)  fornecedor id {f.get('id')}  itens {len(det.get('itens') or [])}")
    time.sleep(0.4)
if not pcs:
    print("  (não achei pelo número)")

print(f"== cadastros no Bling com o CNPJ {dig(a.cnpj)}")
cts = g("/contatos", numeroDocumento=dig(a.cnpj), limite=20)
cts2 = g("/contatos", numeroDocumento=a.cnpj, limite=20) if a.cnpj != dig(a.cnpj) else []
vistos = set()
for c in cts + cts2:
    if c["id"] in vistos:
        continue
    vistos.add(c["id"])
    bate = dig(c.get("numeroDocumento")) == dig(a.cnpj)
    print(f"  contato id {c['id']}  {c.get('nome')}  doc '{c.get('numeroDocumento')}'  {'(bate)' if bate else '(CNPJ diferente!)'}  situação {c.get('situacao')}")
    for sit in (0, 3):
        time.sleep(0.4)
        lst = g("/pedidos/compras", **{"idFornecedor": c["id"], "idsSituacoes[]": sit, "limite": 50})
        print(f"      situação {sit}: {len(lst)} pedido(s): " + ", ".join(f"{p.get('numero')} ({p.get('data')})" for p in lst[:12]))
if not vistos:
    print("  (nenhum contato com esse CNPJ pela busca numeroDocumento)")
