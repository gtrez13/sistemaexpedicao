#!/usr/bin/env python3
"""Remendo 27/11 (2): código do fornecedor comparado SEM zeros à esquerda (NF '002518' x pedido '2518' — Nalgon/SILAB, pedido 3255).
Edita recebimento_routes.py no lugar com âncoras exatas; exige o remendo_receb_desc.py antes. Backup .bak_zeros_<ts>."""
import sys, time, shutil
p = sys.argv[1] if len(sys.argv) > 1 else "recebimento_routes.py"
s = open(p, encoding="utf-8").read()
if "_cod(" in s and "def _cod(" in s:
    print("já remendado"); sys.exit(0)
if "_desc_key(" not in s:
    print("rode antes o remendo_receb_desc.py"); sys.exit(2)
R = [
('''def _mesmo_prod(a, b):
    return bool((a.get("sku") and b.get("sku") and _up(a["sku"]) == _up(b["sku"])) or
                (a.get("cprod") and b.get("cprod") and _up(a["cprod"]) == _up(b["cprod"])))
''', '''def _cod(v):
    """Código do fornecedor para comparar: maiúsculo e SEM zeros à esquerda (NF '002518' = pedido '2518')."""
    u = _up(v)
    return u.lstrip("0") or u


def _mesmo_prod(a, b):
    return bool((a.get("sku") and b.get("sku") and _up(a["sku"]) == _up(b["sku"])) or
                (a.get("cprod") and b.get("cprod") and _cod(a["cprod"]) == _cod(b["cprod"])))
'''),
('''             or next((g for g in grupos if any(x.get("cprod") and o.get("cprod") and _up(x["cprod"]) == _up(o["cprod"]) for o in g["nf"])), None))
        if not g:   # 3) mesma DESCRIÇÃO''',
 '''             or next((g for g in grupos if any(x.get("cprod") and o.get("cprod") and _cod(x["cprod"]) == _cod(o["cprod"]) for o in g["nf"])), None))
        if not g:   # 3) mesma DESCRIÇÃO'''),
('''            pi = next((x for x in pc["itens"] if x["cprod"] and str(x["cprod"]).upper() == str(it["cprod"]).upper() and x["sku"]), None)
        origem = "pedido"''',
 '''            pi = next((x for x in pc["itens"] if x["cprod"] and _cod(x["cprod"]) == _cod(it["cprod"]) and x["sku"]), None)
        origem = "pedido"'''),
]
for a, b in R:
    if s.count(a) != 1:
        print(f"âncora não encontrada: {a[:70]!r}"); sys.exit(2)
for a, b in R:
    s = s.replace(a, b)
bak = f"{p}.bak_zeros_{time.strftime('%Y%m%d%H%M%S')}"
shutil.copy2(p, bak); open(p, "w", encoding="utf-8").write(s); compile(s, p, "exec")
print(f"OK remendado. backup: {bak}")
