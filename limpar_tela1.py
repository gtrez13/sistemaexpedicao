"""Limpa a TELA 1 (Separação): apaga pedidos PENDENTE e PARCIAL (e os itens deles) para sincronizar de novo.
NÃO mexe na tela 2 (SEPARADO) nem no que já saiu (CONCLUIDO/ARQUIVADO).
Antes de apagar, copia tudo para tabelas de backup. Desfazer: venv/bin/python -u limpar_tela1.py --restaurar <carimbo>
Uso: venv/bin/python -u limpar_tela1.py            -> só mostra
     venv/bin/python -u limpar_tela1.py --apagar   -> backup + apaga"""
import sys
import time

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from db import db_session

TELA1 = "('PENDENTE','PARCIAL')"
args = sys.argv[1:]

def rodar(c):
    if args[:1] == ["--restaurar"]:
        ts = args[1]
        c.execute(f"INSERT INTO pedidos SELECT * FROM bkp_tela1_pedidos_{ts} ON CONFLICT DO NOTHING")
        c.execute(f"INSERT INTO pedido_produtos SELECT * FROM bkp_tela1_pedido_produtos_{ts} ON CONFLICT DO NOTHING")
        n = c.execute(f"SELECT COUNT(*) AS n FROM bkp_tela1_pedidos_{ts}").fetchone()["n"]
        print(f"Restaurados {n} pedidos da tela 1 (backup {ts}).")
        return

    print("Situação atual (todas as telas):")
    for r in c.execute("SELECT status, COALESCE(origem,'?') AS origem, COUNT(*) AS n FROM pedidos GROUP BY status, origem ORDER BY status, origem").fetchall():
        print(f"   {r['status']:<10} {r['origem']:<10} {r['n']}")
    n = c.execute(f"SELECT COUNT(*) AS n FROM pedidos WHERE status IN {TELA1}").fetchone()["n"]
    print(f"\nTela 1 (PENDENTE + PARCIAL): {n} pedidos")
    if "--apagar" not in args:
        print("Nada apagado. Para apagar: venv/bin/python -u limpar_tela1.py --apagar")
        return
    ts = time.strftime("%Y%m%d%H%M%S")
    c.execute(f"CREATE TABLE bkp_tela1_pedidos_{ts} AS SELECT * FROM pedidos WHERE status IN {TELA1}")
    c.execute(f"CREATE TABLE bkp_tela1_pedido_produtos_{ts} AS SELECT * FROM pedido_produtos WHERE pedido_id IN (SELECT id FROM pedidos WHERE status IN {TELA1})")
    c.execute(f"DELETE FROM pedido_produtos WHERE pedido_id IN (SELECT id FROM pedidos WHERE status IN {TELA1})")
    c.execute(f"DELETE FROM pedidos WHERE status IN {TELA1}")
    print(f"\nAPAGADOS {n} pedidos da tela 1 (backup nas tabelas bkp_tela1_*_{ts}).")
    print("Agora abra a Separação e toque em 'Começar o dia' para sincronizar de novo.")
    print(f"Para desfazer: venv/bin/python -u limpar_tela1.py --restaurar {ts}")


with db_session() as conn:
    rodar(conn)
