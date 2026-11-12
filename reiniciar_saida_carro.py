"""Recomeça o carro de HOJE na Saída: apaga os bipes de hoje dos pacotes desse carro (para bipar tudo de novo).
Antes guarda tudo numa tabela de backup; 1 comando desfaz. O histórico (saida_log) NÃO é apagado.
Uso:
  venv/bin/python -u reiniciar_saida_carro.py COLETA            # ou FLEX, SHOPEE, COMERCIAL
  venv/bin/python -u reiniciar_saida_carro.py --restaurar <ts>  # desfaz
"""
import sys
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import mapa_routes as m
from db import db_session

args = sys.argv[1:]
if not args:
    sys.exit("Passe o carro: COLETA, FLEX, SHOPEE ou COMERCIAL")

if args[0] == "--restaurar":
    ts = args[1] if len(args) > 1 and args[1].isdigit() else sys.exit("Passe o código do backup (ts, só números)")
    with db_session() as c:
        n0 = c.execute(f"SELECT COUNT(*) AS n FROM saida_bipes_bkp_{ts}").fetchone()["n"]
        c.execute(f"INSERT INTO saida_bipes (ml_id, codigo, por, em) SELECT ml_id, codigo, por, em FROM saida_bipes_bkp_{ts} "
                  "WHERE ml_id NOT IN (SELECT ml_id FROM saida_bipes)")
        c.execute("INSERT INTO saida_log (ml_id, cliente, acao, codigo, tipo, carro, por, em, detalhe) VALUES (?,?,?,?,?,?,?,?,?)",
                  (None, None, "restaurou", None, None, None, "sistema", int(time.time()), f"voltou {n0} bipe(s) do backup {ts}"))
    print(f"OK — {n0} bipe(s) de volta (os que foram bipados de novo depois ficaram como estão).")
    sys.exit(0)

carro = args[0].upper()
if carro not in ("COLETA", "FLEX", "SHOPEE", "COMERCIAL"):
    sys.exit("Carro inválido: use COLETA, FLEX, SHOPEE ou COMERCIAL")
if not hasattr(m, "_bipados_hoje"):
    sys.exit("Instale antes o backend novo da Saída (atualizar_mapa_foto.sh 36ec734 ou mais novo).")
ts = time.strftime("%Y%m%d%H%M%S")
with db_session() as c:
    m._saida_tabelas(c)
    ids = m._bipados_hoje(c, carro)
    if not ids:
        sys.exit(f"Nenhum bipe de hoje no {carro}. Nada a fazer.")
    marcas = ",".join("?" * len(ids))
    c.execute(f"CREATE TABLE saida_bipes_bkp_{ts} AS SELECT * FROM saida_bipes WHERE ml_id IN ({marcas})", tuple(ids))
    for r in c.execute(f"SELECT b.ml_id, b.por, b.em FROM saida_bipes b WHERE b.ml_id IN ({marcas}) ORDER BY b.em", tuple(ids)).fetchall():
        print(f"   {r['ml_id']:20} bipado {time.strftime('%H:%M', time.gmtime(int(r['em']) - 3 * 3600))} por {r['por']}")
    c.execute(f"DELETE FROM saida_bipes WHERE ml_id IN ({marcas})", tuple(ids))
    c.execute("INSERT INTO saida_log (ml_id, cliente, acao, codigo, tipo, carro, por, em, detalhe) VALUES (?,?,?,?,?,?,?,?,?)",
              (None, None, "reiniciou", None, carro, carro, "sistema", int(time.time()), f"{len(ids)} bipe(s) de hoje apagados para bipar de novo (backup {ts})"))
print(f"\nOK — {len(ids)} bipe(s) de hoje do {carro} apagados. Atualize a tela da Saída e bipe tudo de novo.")
print(f"Para desfazer: venv/bin/python -u reiniciar_saida_carro.py --restaurar {ts}")
