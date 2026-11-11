"""Pedido travado na Expedição (bipou a última unidade e deu erro): mostra o estado no WMS e no Bling.
Uso: venv/bin/python -u destravar_pedido.py <ml_id>                 -> só mostra
     venv/bin/python -u destravar_pedido.py <ml_id> --desbipar-1    -> volta 1 unidade para o operador bipar de novo
                                                                      (o fechamento roda de novo; se a NF já existir, o Bling reaproveita)"""
import sys

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from db import db_session

ml_id = sys.argv[1]
desbipar = "--desbipar-1" in sys.argv[2:]


def rodar(c):
    p = c.execute("SELECT id, ml_id, status, origem, cliente_nome FROM pedidos WHERE ml_id=?", (ml_id,)).fetchone()
    if not p:
        print("pedido não está no WMS")
        return
    print(f"WMS: pedido {p['ml_id']} | {p['cliente_nome']} | status {p['status']} | origem {p['origem']}")
    its = c.execute("SELECT pp.id, pr.sku, pr.nome, pp.quantidade, pp.quantidade_bipada FROM pedido_produtos pp "
                    "JOIN produtos pr ON pr.id=pp.produto_id WHERE pp.pedido_id=? ORDER BY pp.id", (p["id"],)).fetchall()
    for i in its:
        print(f"   {i['sku']} | {i['nome']} | bipado {i['quantidade_bipada']}/{i['quantidade']}")
    try:
        from services.bling_service import BlingService
        b = BlingService()
        pv = (b._get_json("/pedidos/vendas", params={"numerosLojas[]": ml_id, "limite": 5}).get("data") or [])
        if not pv:
            print("Bling: pedido de venda não encontrado")
        else:
            det = b._get_json(f"/pedidos/vendas/{pv[0]['id']}").get("data") or {}
            nf_id = (det.get("notaFiscal") or {}).get("id")
            print(f"Bling: PV {det.get('numero')} | situação {(det.get('situacao') or {}).get('id')} | NF id {nf_id or '-'}")
            if nf_id:
                nf = b._get_json(f"/nfe/{nf_id}").get("data") or {}
                print(f"Bling: NF {nf.get('numero')} série {nf.get('serie')} | situação {nf.get('situacao')} (5/6 = autorizada) | chave {nf.get('chaveAcesso') or '-'}")
    except Exception as e:
        print("Bling: não consegui consultar:", str(e)[:200])

    if not desbipar:
        return
    if p["status"] == "CONCLUIDO":
        print("Pedido já CONCLUIDO: não mexi. Use 'Já saíram hoje' > reimprimir etiqueta.")
        return
    cheio = [i for i in its if i["quantidade_bipada"] >= i["quantidade"] and i["quantidade_bipada"] > 0]
    if len(cheio) != len(its):
        print("Ainda falta bipar item: não precisa desbipar, é só bipar o que falta.")
        return
    alvo = cheio[-1]
    c.execute("UPDATE pedido_produtos SET quantidade_bipada = quantidade_bipada - 1 WHERE id=? AND quantidade_bipada > 0", (alvo["id"],))
    print(f"\nPRONTO: voltei 1 unidade de {alvo['sku']}. Recarregue a Expedição e bipe essa última unidade de novo.")
    print(f"Para desfazer: o próprio bip devolve a unidade.")


with db_session() as conn:
    rodar(conn)
