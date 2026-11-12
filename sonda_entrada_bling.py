"""SÓ LEITURA. Mostra o que o Bling tem para dar ENTRADA de uma nota conferida no WMS:
nota do fornecedor já existe no Bling? pedido de compra (situação, parcelas, itens), controle de lote dos produtos,
duplicatas do XML e se as rotas de lançar estoque/contas do pedido de compra existem (testa com id 0, que não existe).
Uso: venv/bin/python -u sonda_entrada_bling.py <chave de 44 dígitos ou nº da NF>"""
import json
import re
import sys
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import requests
from services.bling_service import BlingService
from db import db_session

arg = re.sub(r"\D", "", sys.argv[1] if len(sys.argv) > 1 else "")
if not arg:
    sys.exit("Passe a chave (44 dígitos) ou o número da NF.")
b = BlingService()


def bruto(metodo, caminho, params=None):
    try:
        r = requests.request(metodo, f"{b.base_url}{caminho}", headers=b._headers(), params=params, timeout=30)
        return r.status_code, r.text[:600]
    except Exception as e:
        return 0, str(e)


def mostra(titulo, obj, n=3500):
    print(f"\n== {titulo} ==")
    print(json.dumps(obj, ensure_ascii=False, indent=1)[:n] if not isinstance(obj, str) else obj[:n])


with db_session() as c:
    if len(arg) == 44:
        r = c.execute("SELECT * FROM receb_notas WHERE chave=?", (arg,)).fetchone()
    else:
        r = c.execute("SELECT * FROM receb_notas WHERE numero=? ORDER BY id DESC", (arg.lstrip("0"),)).fetchone() \
            or c.execute("SELECT * FROM receb_notas WHERE numero=? ORDER BY id DESC", (arg,)).fetchone()
    if not r:
        sys.exit("Essa nota não está no Recebimento do WMS.")
    nota = {k: r[k] for k in r.keys() if k != "xml"}
    xml = r["xml"] or ""
    itens = [dict(x) for x in c.execute("SELECT n_item, cprod, sku, descricao, qtd_nf, qtd_conferida, lote, validade FROM receb_itens "
                                        "WHERE nota_id=? ORDER BY n_item", (r["id"],)).fetchall()]
print(f"NF {nota['numero']}/{nota['serie']} · {nota['fornecedor_nome']} · status WMS: {nota['status']} · pedido {nota['pedido_compra_numero']} (id {nota['pedido_compra_id']})")
print(f"chave {nota['chave']}")

# 1) duplicatas do XML (contas a pagar)
dups = re.findall(r"<dup>(.*?)</dup>", xml, re.S)
print("\n== Duplicatas no XML (viram contas a pagar) ==")
for d in dups:
    g = lambda t: (re.search(rf"<{t}>(.*?)</{t}>", d) or [None, ""])[1]
    print(f"   {g('nDup')}  vence {g('dVenc')}  R$ {g('vDup')}")
if not dups:
    print("   (o XML não tem duplicatas)")
print("   forma de pagamento no XML:", ", ".join(re.findall(r"<tPag>(\d+)</tPag>", xml)) or "-")

# 2) a nota do fornecedor já está no Bling? (entrada importada)
print("\n== Nota do fornecedor no Bling (notas de ENTRADA com esse número) ==")
achou = None
for params in ({"tipo": 0, "numero": nota["numero"]}, {"numero": nota["numero"]}):
    try:
        lst = b._get_json("/nfe", params={**params, "limite": 50}).get("data") or []
    except Exception as e:
        print(f"   busca {params} recusada: {str(e)[:150]}")
        continue
    for n in lst:
        print(f"   id {n.get('id')} · nº {n.get('numero')}/{n.get('serie')} · tipo {n.get('tipo')} · situação {n.get('situacao')} · "
              f"contato {(n.get('contato') or {}).get('nome')} · chave {n.get('chaveAcesso')}")
        if re.sub(r'\D', '', str(n.get('chaveAcesso') or '')) == nota["chave"]:
            achou = n
    if lst:
        break
if achou:
    det = b._get_json(f"/nfe/{achou['id']}").get("data") or {}
    det.pop("xml", None)
    mostra("NOTA DO FORNECEDOR NO BLING (estrutura)", det, 3000)
else:
    print("   >> a nota do fornecedor AINDA NÃO está no Bling (ninguém importou o XML)")

# 3) pedido de compra
if nota["pedido_compra_id"]:
    pc = b._get_json(f"/pedidos/compras/{nota['pedido_compra_id']}").get("data") or {}
    mostra(f"PEDIDO DE COMPRA {nota['pedido_compra_numero']} (estrutura)", pc, 4000)

# 4) controle de lote dos produtos
print("\n== Produtos da nota: controle de lote no Bling ==")
for it in itens:
    if not it["sku"]:
        print(f"   item {it['n_item']}: sem SKU")
        continue
    try:
        pr = (b._get_json("/produtos", params={"codigo": it["sku"]}).get("data") or [])
        pid = pr[0]["id"] if pr else None
        cl = b._get_json("/produtos/lotes/controla-lote", params={"idsProdutos[]": pid}).get("data") if pid else None
        lotes = b._get_json("/produtos/lotes", params={"idsProdutos[]": pid}).get("data") if pid else None
        print(f"   item {it['n_item']} · {it['sku']} (id {pid}) · controla lote: {cl} · lotes existentes: {len(lotes or [])} · "
              f"WMS lote {it['lote'] or '-'} val {it['validade'] or '-'} · conferido {it['qtd_conferida']}/{it['qtd_nf']}")
    except Exception as e:
        print(f"   item {it['n_item']} · {it['sku']}: erro {str(e)[:150]}")

# 5) rotas existem? (id 0 não existe: 'não encontrado' = rota existe; 'rota/recurso inexistente' = não existe)
print("\n== Rotas do Bling (teste com id 0, não mexe em nada) ==")
for met, cam in (("POST", "/pedidos/compras/0/lancar-estoque"), ("POST", "/pedidos/compras/0/lancar-contas"),
                 ("PATCH", "/pedidos/compras/0/situacoes/1"), ("GET", "/contas/pagar"), ("GET", "/formas-pagamentos"),
                 ("GET", "/categorias/receitas-despesas")):
    st, txt = bruto(met, cam, params={"limite": 1} if met == "GET" else None)
    print(f"   {met} {cam} -> HTTP {st} {txt[:220]}")
print("\nNada foi criado nem alterado.")
