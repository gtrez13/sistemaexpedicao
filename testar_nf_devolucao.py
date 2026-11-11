"""TESTE da nota de devolução — SÓ LEITURA: não cria nota, não mexe em estoque, não grava nada.
Roda a mesma checagem do botão da prancheta e mostra, por devolução, o que passaria e o que travaria.
No fim, junta os problemas (o "mapa de pontos") para corrigir antes de apresentar.

Uso:
  venv/bin/python -u testar_nf_devolucao.py            # últimas 15 devoluções conferidas sem nota
  venv/bin/python -u testar_nf_devolucao.py 30         # últimas 30
  venv/bin/python -u testar_nf_devolucao.py id 123     # só a devolução #123, com o JSON que iria para o Bling
"""
import json
import sys
from collections import Counter

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import devolucoes_routes as d
from db import db_session

d.garantir_tabelas()
args = sys.argv[1:]
so_id = int(args[1]) if len(args) >= 2 and args[0] == "id" and args[1].isdigit() else None
limite = int(args[0]) if args and args[0].isdigit() else 15

with db_session() as c:
    if so_id:
        rows = c.execute("SELECT * FROM devolucoes WHERE id=?", (so_id,)).fetchall()
    else:
        rows = [r for r in c.execute("SELECT * FROM devolucoes ORDER BY id DESC LIMIT 300").fetchall()
                if r["bipado"] and r["chegada"] and not r["nf_dev_id"] and r["status"] != "armazem_ml"][:limite]
if not rows:
    sys.exit("Nenhuma devolução conferida sem nota para testar. Conclua uma prancheta e rode de novo (ou use: id <número>).")

MARCA = {True: "[ok]", False: "[X] ", None: "[!] "}
mapa, prontas = Counter(), 0
for r in rows:
    print(f"\n#{r['id']}  {r['cliente'] or '-'}  · venda {r['numero_venda']}  · {r['sku']}  · {r['chegada']}")
    try:
        p = d._plano_nf_dev(r)
    except Exception as e:
        print(f"   [X]  erro montando a nota: {e}")
        mapa["erro montando a nota"] += 1
        continue
    for pt in p["pontos"]:
        print(f"   {MARCA[pt['ok']]} {pt['item']}: {pt['detalhe']}")
        if pt["ok"] is not True:
            mapa[f"{'TRAVA' if pt['ok'] is False else 'aviso'} · {pt['item']}"] += 1
    if p["bloqueio"]:
        print(f"   => NÃO gera: {p['bloqueio']}")
    elif p["dep"] not in d.NOME_DEP:
        print("   => gera depois que o operador escolher o depósito")
        prontas += 1
    else:
        print(f"   => GERARIA a nota (pendente) e lançaria em {d.NOME_DEP[p['dep']]}")
        prontas += 1
    if so_id and p["payloads"]:
        print("\n== JSON que iria para o Bling (POST /nfe) ==")
        print(json.dumps(p["payloads"][0], ensure_ascii=False, indent=1))

print(f"\n== MAPA DE PONTOS ({len(rows)} devoluções testadas, {prontas} gerariam) ==")
if not mapa:
    print("   nenhum problema encontrado")
for k, n in mapa.most_common():
    print(f"   {n:>3}x  {k}")
print("\nNada foi criado nem alterado.")
