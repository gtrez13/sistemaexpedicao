"""Só leitura: mostra o que o Bling tem "Em andamento" e o que a sincronização do Comercial traria.
Uso: venv/bin/python -u testar_comercial.py [dias=7]"""
import sys
from datetime import date, datetime, timedelta

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from services.bling_service import BlingService

dias = int(sys.argv[1]) if len(sys.argv) > 1 else 7
b = BlingService()

print("1) No Bling, situação 'Em andamento' (15), últimos 60 dias:")
for pag in range(1, 6):
    d = b._get_json("/pedidos/vendas", params={
        "pagina": pag, "limite": 100, "idsSituacoes[]": [15],
        "dataInicial": (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"),
        "dataFinal": date.today().strftime("%Y-%m-%d")}, timeout=(10, 30))
    itens = d.get("data") or []
    for pv in itens:
        print(f"   PV {pv.get('numero')} | {pv.get('data')} | {(pv.get('contato') or {}).get('nome')} | "
              f"situação {(pv.get('situacao') or {}).get('id')} | loja {(pv.get('loja') or {}).get('id')} | numeroLoja {pv.get('numeroLoja') or '-'}")
    if len(itens) < 100:
        break

print(f"\n2) O que a sincronização do Comercial traria agora (dias={dias}, igual ao botão):")
vendas = b.buscar_vendas_internas(dias=dias)
for v in vendas:
    print(f"   PV {v.get('bling_numero')} | {v.get('cliente_nome')} | {len(v.get('produtos') or [])} produto(s) | origem {v.get('origem')}")
print(f"   total: {len(vendas)}")
