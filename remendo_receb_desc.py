#!/usr/bin/env python3
"""Remendo 27/11: NF x pedido de compra também casa pela DESCRIÇÃO exata (quando SKU e código do fornecedor não batem).
Caso real: Nalgon/SILAB NF x pedido 3255 — 7 itens com descrição idêntica no pedido e na nota, cProd diferente do
'código do fornecedor' do pedido -> 21 linhas de divergência e 'sem SKU vinculado'.
Edita recebimento_routes.py NO LUGAR, com âncoras exatas; se alguma âncora não existir, não mexe em nada.
Uso: python3 remendo_receb_desc.py [recebimento_routes.py]   (faz .bak_desc_<ts>)"""
import re, sys, time, shutil

p = sys.argv[1] if len(sys.argv) > 1 else "recebimento_routes.py"
s = open(p, encoding="utf-8").read()
if "_desc_key(" in s:
    print("já remendado"); sys.exit(0)

R = []
# 1) helpers depois de _mesmo_prod
R.append(('''def _mesmo_prod(a, b):
    return bool((a.get("sku") and b.get("sku") and _up(a["sku"]) == _up(b["sku"])) or
                (a.get("cprod") and b.get("cprod") and _up(a["cprod"]) == _up(b["cprod"])))
''', '''def _mesmo_prod(a, b):
    return bool((a.get("sku") and b.get("sku") and _up(a["sku"]) == _up(b["sku"])) or
                (a.get("cprod") and b.get("cprod") and _up(a["cprod"]) == _up(b["cprod"])))


def _desc_key(t):
    """Descrição normalizada para comparar NF x pedido (sem acento, só letras e números)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", t)


def _pc_por_descricao(it, pc_itens, grupos_nf=None):
    """Item do pedido com a MESMA descrição do item da nota — só quando é única dos dois lados (sem chute).
    27/11: Nalgon manda cProd diferente do 'código do fornecedor' do pedido, mas a descrição é idêntica."""
    k = _desc_key(it.get("descricao"))
    if len(k) < 6:
        return None
    def bate(a, b):   # igual, ou uma começa com a outra (pedido com observação depois do nome)
        return a == b or (len(a) >= 12 and len(b) >= 12 and (a.startswith(b) or b.startswith(a)))
    cands = [x for x in pc_itens if bate(_desc_key(x.get("descricao")), k)]
    if len(cands) != 1:
        return None
    kp = _desc_key(cands[0].get("descricao"))
    if grupos_nf is not None and sum(1 for g in grupos_nf if bate(kp, _desc_key(g["nf"][0].get("descricao")))) != 1:
        return None
    return cands[0]
'''))
# 2) _grupos: 3º critério = descrição
R.append(('''        g = (next((g for g in grupos if any(x.get("sku") and o.get("sku") and _up(x["sku"]) == _up(o["sku"]) for o in g["nf"])), None)
             or next((g for g in grupos if any(x.get("cprod") and o.get("cprod") and _up(x["cprod"]) == _up(o["cprod"]) for o in g["nf"])), None))
        if g:
            g["pc"].append(x)
        else:
            sobras.append(x)
    return grupos, sobras
''', '''        g = (next((g for g in grupos if any(x.get("sku") and o.get("sku") and _up(x["sku"]) == _up(o["sku"]) for o in g["nf"])), None)
             or next((g for g in grupos if any(x.get("cprod") and o.get("cprod") and _up(x["cprod"]) == _up(o["cprod"]) for o in g["nf"])), None))
        if not g:   # 3) mesma DESCRIÇÃO, única dos dois lados (27/11)
            g = next((g for g in grupos if not g["pc"] and not any(o.get("sku") for o in g["nf"])
                      and _pc_por_descricao(g["nf"][0], (pc or {}).get("itens") or [], grupos) is x), None)
        if g:
            g["pc"].append(x)
        else:
            sobras.append(x)
    return grupos, sobras
'''))
# 3) herdar SKU também pela descrição
R.append(('''    for it in d["itens"]:
        if it["sku"] or not it["cprod"]:
            continue
        pi = next((x for x in pc["itens"] if x["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper() and x["sku"]), None)
        if pi:
            c.execute("UPDATE receb_itens SET sku=? WHERE id=?", (pi["sku"], it["id"]))
            c.execute("""INSERT INTO receb_vinculos (fornecedor_cnpj, cprod, sku, criado_por, criado_em) VALUES (?,?,?,?,?)
                         ON CONFLICT (fornecedor_cnpj, cprod) DO NOTHING""", (d["fornecedor_cnpj"], it["cprod"], pi["sku"], "pedido", _agora()))
            mudou = True
    return mudou
''', '''    grupos, _ = _grupos(d["itens"], pc)
    for it in d["itens"]:
        if it["sku"]:
            continue
        pi = None
        if it["cprod"]:
            pi = next((x for x in pc["itens"] if x["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper() and x["sku"]), None)
        origem = "pedido"
        if not pi:   # 27/11: mesma descrição (única) no pedido
            pi = _pc_por_descricao(it, pc["itens"], grupos)
            origem = "descricao"
            if pi and not pi.get("sku"):
                pi = None
        if pi:
            c.execute("UPDATE receb_itens SET sku=? WHERE id=?", (pi["sku"], it["id"]))
            it["sku"] = pi["sku"]
            if it["cprod"]:
                c.execute("""INSERT INTO receb_vinculos (fornecedor_cnpj, cprod, sku, criado_por, criado_em) VALUES (?,?,?,?,?)
                             ON CONFLICT (fornecedor_cnpj, cprod) DO NOTHING""", (d["fornecedor_cnpj"], it["cprod"], pi["sku"], origem, _agora()))
            print(f"[RECEB] item {it['n_item']} ({str(it.get('descricao'))[:30]}) herdou SKU {pi['sku']} do pedido por {origem}", flush=True)
            mudou = True
    return mudou
'''))
# 4) mensagem diz qual código veio na NF
R.append(('''            m.append(f"Item {it['n_item']} ({nome}): sem SKU vinculado")
''', '''            m.append(f"Item {it['n_item']} ({nome}): sem SKU vinculado (código do fornecedor na NF: {it.get('cprod') or '-'}"
                     f"{', EAN ' + it['ean'] if it.get('ean') else ''}) — toque no item e escolha o SKU")
'''))

for a, b in R:
    n = s.count(a)
    if n != 1:
        print(f"âncora não encontrada ({n}x): {a[:70]!r}"); sys.exit(2)
for a, b in R:
    s = s.replace(a, b)
bak = f"{p}.bak_desc_{time.strftime('%Y%m%d%H%M%S')}"
shutil.copy2(p, bak)
open(p, "w", encoding="utf-8").write(s)
compile(s, p, "exec")
print(f"OK remendado. backup: {bak}")
