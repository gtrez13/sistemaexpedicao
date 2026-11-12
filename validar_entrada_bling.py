"""VALIDA, NO BLING DE VERDADE, se a API consegue fazer a entrada de uma nota conferida, na ordem do Biel:
  1) conciliar o pedido de compra com a nota   2) subir a nota de entrada   3) produtos e estoque (+ lotes)   4) pedido Atendido e contas
NÃO cria, NÃO altera, NÃO apaga nada: só lê, e testa cada rota com id 0 (que não existe) ou com corpo vazio
(o Bling recusa antes de gravar). Comparando com uma rota inventada, dá para saber se a rota existe e se o app tem permissão.
Uso: venv/bin/python -u validar_entrada_bling.py <nº da NF ou chave de 44 dígitos>"""
import json
import re
import sys
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import recebimento_routes as rr
from db import db_session

arg = re.sub(r"\D", "", sys.argv[1] if len(sys.argv) > 1 else "")
if not arg:
    sys.exit("Passe o nº da NF ou a chave de 44 dígitos.")
rr.garantir_tabelas()
OK, NAO, AV = "[ok]", "[X] ", "[!] "
resumo = []


def linha(marca, texto):
    print(f"   {marca} {texto}")
    if marca != OK:
        resumo.append(f"{marca} {texto}")


_raw0 = rr._bling_raw


def _raw(metodo, caminho, payload=None, params=None):
    """Igual ao do WMS, mas se o Bling disser 'muitas requisições' (429) espera e tenta de novo (senão o teste engana)."""
    import time as _t
    for k in range(4):
        st, txt, js = _raw0(metodo, caminho, payload=payload, params=params)
        if st != 429:
            return st, txt, js
        _t.sleep(2 + k)
    return st, txt, js


rr._bling_raw = _raw


def erro_bl(js, txt):
    e = (js or {}).get("error") or {}
    return f"{e.get('type') or ''} {e.get('message') or ''} {e.get('description') or ''}".strip() or (txt or "")[:160]


# controle: rota que com certeza NÃO existe (para comparar a resposta)
st_c, txt_c, js_c = rr._bling_raw("POST", "/rota-que-nao-existe-wms-teste")
assin_c = (st_c, erro_bl(js_c, txt_c))


def rota(metodo, caminho, payload=None, o_que=""):
    """-> True (existe e tem permissão) / False (não existe ou sem permissão) / None (incerto)"""
    st, txt, js = rr._bling_raw(metodo, caminho, payload=payload)
    msg = erro_bl(js, txt)
    if st in (200, 201, 204):
        linha(AV, f"{o_que}: {metodo} {caminho} respondeu {st} (não esperado num teste) — {msg[:120]}")
        return True
    if st == 401:
        linha(NAO, f"{o_que}: token do Bling inválido/vencido (HTTP 401)")
        return False
    if st == 403:
        linha(NAO, f"{o_que}: {metodo} {caminho} SEM PERMISSÃO no app do Bling (HTTP 403 {msg[:120]}) — liberar o escopo no app")
        return False
    if (st, msg) == assin_c:
        # o Bling responde igual para "id não existe" e "rota não existe": tenta 2 testes a mais, também sem gravar
        for m2, c2, ctl in ((metodo, re.sub(r"/0(?=/|$)", "/abc", caminho), (metodo, "/rota-que-nao-existe-wms-teste/abc")),
                            ("GET", caminho, ("GET", "/rota-que-nao-existe-wms-teste"))):
            a = rr._bling_raw(m2, c2)
            b = rr._bling_raw(ctl[0], ctl[1])
            sa, sb = (a[0], erro_bl(a[2], a[1])), (b[0], erro_bl(b[2], b[1]))
            if sa != sb and a[0] not in (401, 429) and b[0] not in (401, 429):
                if a[0] == 403:
                    linha(NAO, f"{o_que}: {metodo} {caminho} existe, mas o app está SEM PERMISSÃO (403 {sa[1][:90]})")
                    return False
                linha(OK, f"{o_que}: {metodo} {caminho} existe (confirmado: {m2} {c2} -> {sa[0]} {sa[1][:70]}; rota inventada -> {sb[0]})")
                return True
        linha(AV, f"{o_que}: {metodo} {caminho} — o Bling responde igual a uma rota inventada ({st}) em todos os testes sem gravar. "
                  "Está na especificação oficial; só a 1ª entrada real confirma")
        return None
    linha(OK, f"{o_que}: {metodo} {caminho} existe e o app tem permissão (teste recusado como esperado: {st} {msg[:90]})")
    return True


# ── nota no WMS ──
with db_session() as c:
    r = (c.execute("SELECT * FROM receb_notas WHERE chave=?", (arg,)).fetchone() if len(arg) == 44 else
         c.execute("SELECT * FROM receb_notas WHERE numero=? OR numero=? ORDER BY id DESC", (arg.lstrip("0"), arg)).fetchone())
    if not r:
        sys.exit("Essa nota não está no Recebimento do WMS.")
    nid = r["id"]
print(f"NF {r['numero']}/{r['serie']} · {r['fornecedor_nome']} · WMS: {r['status']} · pedido {r['pedido_compra_numero'] or '-'}")
print(f"(controle: rota inventada responde {st_c} {assin_c[1][:80]})")

# ── 1) CONCILIAR pedido de compra x nota ──
print("\n== 1) Conciliar o pedido de compra com a nota ==")
rr.pedidos_compra_do_fornecedor(r["fornecedor_cnpj"], fresco=True)
with db_session() as c:
    d = rr._nota_dict(c, nid)
if not d.get("pedido_compra"):
    linha(NAO, "nenhum pedido de compra em aberto vinculado a essa nota" + (f" (o XML diz pedido {d.get('pedido_xml')})" if d.get("pedido_xml") else ""))
else:
    cmp_ = d.get("comparacao") or {}
    div = rr._divergencias(d, conferencia=False)
    if div:
        linha(NAO, f"nota x pedido {d['pedido_compra']['numero']} NÃO bate (tolerância {rr._brl(rr.TOL_TOTAL)}):")
        for m in div:
            print(f"         - {m}")
    else:
        linha(OK, f"nota x pedido {d['pedido_compra']['numero']} bate (diferença total {rr._brl(cmp_.get('dif') or 0)}, aceito até {rr._brl(rr.TOL_TOTAL)})")
    falta = [i for i in d["itens"] if i["qtd_conferida"] < i["qtd_nf"]]
    linha(OK if not falta and r["status"] in ("conferida", "lancada") else AV,
          f"conferência: {r['status']}" + (f" · {len(falta)} item(ns) ainda sem bipar tudo" if falta else ""))
    rota("PUT", "/pedidos/compras/0", {}, "Vincular a nota ao pedido (itens[].notaFiscal do pedido)")

# ── 2) SUBIR a nota de entrada ──
print("\n== 2) Subir a nota de entrada (a NF do fornecedor) no Bling ==")
existe_xml = False
for cam in ("/nfe/importar", "/nfe/xml", "/nfe/importacoes", "/notas-fiscais/entrada", "/nfe/entradas"):
    st, txt, js = rr._bling_raw("POST", cam, payload={})
    if (st, erro_bl(js, txt)) != assin_c and st not in (401, 403):
        existe_xml = True
        print(f"   [!]  {cam} respondeu diferente da rota inventada: {st} {erro_bl(js, txt)[:120]}")
if existe_xml:
    linha(AV, "alguma rota de importar XML respondeu diferente: me mande essa saída")
else:
    linha(NAO, "a API v3 do Bling NÃO tem rota para importar o XML do fornecedor (testei 5 caminhos; a especificação oficial também não tem)")
print("        (POST /nfe com tipo 0 cria nota de EMISSÃO PRÓPRIA da LHVMED, com numeração nossa: NÃO serve para a nota do fornecedor)")
nf_bl = None
for params in ({"chaveAcesso": r["chave"]}, {"tipo": 0, "chaveAcesso": r["chave"]}, {"tipo": 0, "numero": r["numero"]}, {"numero": r["numero"]}):
    st, txt, js = rr._bling_raw("GET", "/nfe", params={**params, "limite": 50})
    for n in ((js or {}).get("data") or []):
        if re.sub(r"\D", "", str(n.get("chaveAcesso") or "")) == r["chave"]:
            nf_bl = n
    if nf_bl:
        break
if nf_bl:
    linha(OK, f"a nota do fornecedor JÁ ESTÁ no Bling (id {nf_bl.get('id')}, situação {nf_bl.get('situacao')}, tipo {nf_bl.get('tipo')})")
else:
    linha(AV, "a nota do fornecedor ainda NÃO está no Bling (ninguém importou o XML)")

# ── 3) PRODUTOS e ESTOQUE ──
print("\n== 3) Produtos e estoque (+ lotes) ==")
plano = rr.plano_entrada(nid)
ids = [p["pid"] for p in plano.get("itens") or [] if p.get("pid")]
saldos = rr._saldos_local(ids) if ids else {}
for p in plano.get("itens") or []:
    if not p.get("pid"):
        linha(NAO, f"item {p['n_item']} {str(p['descricao'])[:40]}: produto não achado no pedido do Bling (SKU {p['sku'] or '-'})")
        continue
    lote = f" · CONTROLA LOTE: lote {p['lote'] or 'FALTA'} val. {p['validade'] or 'FALTA'}" if p["controla_lote"] else ""
    marca = NAO if p["controla_lote"] and not (p["lote"] and p["validade"]) else OK
    linha(marca, f"item {p['n_item']} {p['sku']} (id {p['pid']}): entra {p['qtd']:g} un · saldo Local hoje {saldos.get(int(p['pid']), 0):g}{lote}")
rota("POST", f"/nfe/0/lancar-estoque/{rr.DEP_LOCAL_ID}", None, "Lançar o estoque DA NOTA no Local (quando a nota estiver no Bling)")
rota("POST", "/pedidos/compras/0/lancar-estoque", None, "Lançar o estoque do pedido de compra")
rota("POST", "/estoques", {}, "Lançar estoque produto a produto (entrada no Local)")
rota("POST", "/produtos/lotes/0/lancamentos", {}, "Lançar quantidade no lote")

# ── 4) PEDIDO ATENDIDO e CONTAS ──
print("\n== 4) Pedido Atendido e contas a pagar ==")
sid = rr._situacao_pc("atendido")
linha(OK if sid else NAO, f"situação 'Atendido' dos pedidos de compra: id {sid}" if sid else "não achei a situação 'Atendido' no Bling")
if sid:
    rota("PATCH", f"/pedidos/compras/0/situacoes/{sid}", None, "Mudar o pedido para Atendido")
rota("POST", "/nfe/0/lancar-contas", None, "Lançar as contas DA NOTA (quando a nota estiver no Bling)")
rota("POST", "/pedidos/compras/0/lancar-contas", None, "Lançar as contas do pedido de compra")
rota("POST", "/contas/pagar", {}, "Lançar conta a pagar parcela a parcela")
for p in plano.get("pontos") or []:
    if p["item"] == "Contas a pagar":
        linha(OK if p["ok"] else NAO if p["ok"] is False else AV, f"contas: {p['detalhe']}")

print("\n== RESUMO ==")
if not resumo:
    print("   tudo certo: a API consegue fazer todos os passos dessa nota")
for x in resumo:
    print(f"   {x}")
print("\nNada foi criado, alterado ou apagado no Bling nem no WMS.")
