#!/usr/bin/env python3
"""Remendo 27/11 (3): item marcado 'validade indeterminada' = produto que não tem lote nem vence -> passa SEM lote/série.
Antes exigia lote E validade; produto sem lote travava a conferência (Biel 27/11). Backup .bak_semlote_<ts>."""
import sys, time, shutil
p = sys.argv[1] if len(sys.argv) > 1 else "recebimento_routes.py"
s = open(p, encoding="utf-8").read()
A = '''        if conferencia and EXIGIR_LOTE and (not _tem_lote(it) or not _tem_validade(it)):
            m.append(f"Item {it['n_item']} ({nome}): sem lote/série ou validade (se não vence, marque 'validade indeterminada')")'''
B = '''        # 27/11: 'validade indeterminada' = produto sem lote e que não vence -> não exige lote nem série
        if conferencia and EXIGIR_LOTE and not _indeterminada(it) and (not _tem_lote(it) or not _tem_validade(it)):
            m.append(f"Item {it['n_item']} ({nome}): sem lote/série ou validade (produto sem lote e que não vence: marque 'Sem lote / não vence')")'''
H = '''def _tem_validade(it) -> bool:'''
HB = '''def _indeterminada(it) -> bool:
    return str(it.get("validade_indet") or "") in ("1", "true", "True")


def _tem_validade(it) -> bool:'''
if "_indeterminada(" in s:
    print("já remendado"); sys.exit(0)
for a in (A, H):
    if s.count(a) != 1:
        print(f"âncora não encontrada: {a[:60]!r}"); sys.exit(2)
s = s.replace(A, B).replace(H, HB)
bak = f"{p}.bak_semlote_{time.strftime('%Y%m%d%H%M%S')}"
shutil.copy2(p, bak); open(p, "w", encoding="utf-8").write(s); compile(s, p, "exec")
print(f"OK remendado. backup: {bak}")
