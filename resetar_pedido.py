"""Zera os bips de UM pedido para testar de novo (não mexe no Bling). Backup + desfazer.
Uso: venv/bin/python -u resetar_pedido.py <ml_id ou nº do PV> [--tela 1|2]
     --tela 2 (padrão): volta para a Expedição (SEPARADO, 0 conferidos)
     --tela 1: volta para a Separação (PENDENTE, 0 bipados)
     venv/bin/python -u resetar_pedido.py --desfazer <carimbo>"""
import sys
import time

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from db import db_session

a = sys.argv[1:]


def rodar(c):
    if a[:1] == ["--desfazer"]:
        ts = a[1]
        for r in c.execute(f"SELECT * FROM bkp_reset_pp_{ts}").fetchall():
            c.execute("UPDATE pedido_produtos SET quantidade_bipada=? WHERE id=?", (r["quantidade_bipada"], r["id"]))
        p = c.execute(f"SELECT id, status FROM bkp_reset_ped_{ts}").fetchone()
        c.execute("UPDATE pedidos SET status=? WHERE id=?", (p["status"], p["id"]))
        print("Desfeito.")
        return
    alvo = a[0]
    tela = a[a.index("--tela") + 1] if "--tela" in a else "2"
    novo = "PENDENTE" if tela == "1" else "SEPARADO"
    p = c.execute("SELECT * FROM pedidos WHERE ml_id=?", (alvo,)).fetchone()
    if not p:
        p = c.execute("SELECT * FROM pedidos WHERE CAST(bling_numero AS TEXT)=? ORDER BY id DESC", (alvo,)).fetchone()
    if not p:
        print(f"não achei o pedido {alvo} (nem como ml_id nem como nº do PV)")
        return
    p = dict(p)
    ts = time.strftime("%Y%m%d%H%M%S")
    c.execute(f"CREATE TABLE bkp_reset_ped_{ts} AS SELECT id, status FROM pedidos WHERE id={int(p['id'])}")
    c.execute(f"CREATE TABLE bkp_reset_pp_{ts} AS SELECT id, quantidade_bipada FROM pedido_produtos WHERE pedido_id={int(p['id'])}")
    c.execute("UPDATE pedido_produtos SET quantidade_bipada=0 WHERE pedido_id=?", (p["id"],))
    c.execute("UPDATE pedidos SET status=? WHERE id=?", (novo, p["id"]))
    its = c.execute("SELECT pr.sku, pr.nome, pp.quantidade FROM pedido_produtos pp JOIN produtos pr ON pr.id=pp.produto_id WHERE pp.pedido_id=?", (p["id"],)).fetchall()
    print(f"Pedido {p['ml_id']} | PV {p.get('bling_numero') or '-'} | {p['cliente_nome']} | {p['status']} -> {novo} | bips zerados:")
    for i in its:
        print(f"   {i['sku']} | {i['nome']} | 0/{i['quantidade']}")
    print(f"\nPronto: recarregue a {'Separação' if tela == '1' else 'Expedição'} e bipe de novo.")
    print(f"Para desfazer: venv/bin/python -u resetar_pedido.py --desfazer {ts}")


with db_session() as conn:
    rodar(conn)
