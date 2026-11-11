"""Corrige AGORA, direto no banco, os pedidos do ML da tela 1 que ficaram com o SKU MÃE de um produto com variação:
troca pelo SKU FILHO (cor/tamanho) que vem no próprio pedido. Não precisa sincronizar; o operador só recarrega a página.
Mantém o que já foi bipado. Backup das linhas trocadas + desfazer.
Uso: venv/bin/python -u trocar_sku_filho.py             -> grava
     venv/bin/python -u trocar_sku_filho.py --so-ver    -> só mostra
     venv/bin/python -u trocar_sku_filho.py --desfazer <carimbo>"""
import sys
import time

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from db import db_session
from services.ml_service import MercadoLivreService

args = sys.argv[1:]


def desfazer(c, ts):
    rs = c.execute(f"SELECT pp_id, produto_id_antigo FROM bkp_variacao_{ts}").fetchall()
    for r in rs:
        c.execute("UPDATE pedido_produtos SET produto_id=? WHERE id=?", (r["produto_id_antigo"], r["pp_id"]))
    print(f"Desfeito: {len(rs)} linha(s) voltaram ao SKU anterior.")


def itens_do_ml(ml, ml_id):
    r = ml._request("GET", f"/orders/{ml_id}")
    ordens = []
    if r is not None and r.status_code == 200:
        ordens = [r.json()]
    else:                                             # pode ser o número do pacote
        p = ml._request("GET", f"/packs/{ml_id}")
        if p is not None and p.status_code == 200:
            for o in p.json().get("orders") or []:
                ro = ml._request("GET", f"/orders/{o.get('id')}")
                if ro is not None and ro.status_code == 200:
                    ordens.append(ro.json())
    out = []
    for o in ordens:
        for oi in o.get("order_items") or []:
            it = oi.get("item") or {}
            sku = str(it.get("seller_sku") or it.get("seller_custom_field") or "").strip()
            attrs = " / ".join(f"{a.get('name')}: {a.get('value_name')}" for a in it.get("variation_attributes") or [] if a.get("value_name"))
            out.append({"titulo": (it.get("title") or "Produto").strip(), "sku": sku, "attrs": attrs,
                        "variacao": bool(it.get("variation_id") or it.get("variation_attributes"))})
    return out


def rodar(c):
    if args[:1] == ["--desfazer"]:
        return desfazer(c, args[1])
    gravar = "--so-ver" not in args
    ml = MercadoLivreService()
    rows = [dict(r) for r in c.execute(
        "SELECT pp.*, pr.sku AS sku_atual, pr.nome AS nome_atual, pe.ml_id AS ml_id_ped "
        "FROM pedidos pe JOIN pedido_produtos pp ON pp.pedido_id=pe.id JOIN produtos pr ON pr.id=pp.produto_id "
        "WHERE pe.status IN ('PENDENTE','PARCIAL') AND COALESCE(pe.origem,'ML')='ML'").fetchall()]
    por_pedido = {}
    for r in rows:
        por_pedido.setdefault(r["ml_id_ped"], []).append(r)
    trocas, ts = [], time.strftime("%Y%m%d%H%M%S")
    for ml_id, linhas in por_pedido.items():
        itens = itens_do_ml(ml, ml_id)
        skus_ml = {i["sku"] for i in itens if i["sku"]}
        for i in itens:
            if not i["variacao"] or not i["sku"]:
                continue
            if any(l["sku_atual"] == i["sku"] for l in linhas):
                print(f"ok      pedido {ml_id}: {i['titulo']} [{i['attrs']}] já está com o SKU filho {i['sku']}")
                continue
            cand = [l for l in linhas if l["sku_atual"] not in skus_ml and not l.get("parent_sku") and not l.get("_feito")]
            cand.sort(key=lambda l: 0 if str(l["nome_atual"] or "").lower().startswith(i["titulo"][:15].lower()) else 1)
            if not cand:
                print(f"PULEI   pedido {ml_id}: {i['titulo']} [{i['attrs']}] — não achei a linha com o SKU mãe")
                continue
            l = cand[0]
            l["_feito"] = True
            nome = i["titulo"] + (f" | {i['attrs']}" if i["attrs"] else "")
            print(f"TROCAR  pedido {ml_id}: {l['sku_atual']} ({l['nome_atual']})  ->  {i['sku']} ({nome}) | bipado {l.get('quantidade_bipada', 0)}/{l.get('quantidade')}")
            trocas.append((l, i["sku"], nome))
    if not trocas:
        print("Nada para trocar.")
        return
    if not gravar:
        print("Só mostrei. Para gravar, rode sem --so-ver.")
        return
    c.execute(f"CREATE TABLE bkp_variacao_{ts} (pp_id INTEGER, produto_id_antigo INTEGER)")
    for l, sku, nome in trocas:
        c.execute("INSERT INTO produtos (sku, nome) VALUES (?, ?) ON CONFLICT(sku) DO UPDATE SET nome=excluded.nome", (sku, nome))
        novo = c.execute("SELECT id FROM produtos WHERE sku=?", (sku,)).fetchone()["id"]
        ja = c.execute("SELECT id FROM pedido_produtos WHERE pedido_id=? AND produto_id=?", (l["pedido_id"], novo)).fetchone()
        if ja:
            print(f"PULEI   {sku}: o pedido já tem esse SKU em outra linha")
            continue
        c.execute(f"INSERT INTO bkp_variacao_{ts} (pp_id, produto_id_antigo) VALUES (?, ?)", (l["id"], l["produto_id"]))
        c.execute("UPDATE pedido_produtos SET produto_id=? WHERE id=?", (novo, l["id"]))
    print(f"\nGRAVADO: {len(trocas)} linha(s). O operador só precisa recarregar a página.")
    print(f"Para desfazer: venv/bin/python -u trocar_sku_filho.py --desfazer {ts}")


with db_session() as conn:
    rodar(conn)
