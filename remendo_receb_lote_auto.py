#!/usr/bin/env python3
"""Remendo 27/11 (4): entrada no Bling sem travar por lote/fabricação.
 - Produto que o Bling controla lote mas o item foi marcado 'Sem lote / não vence' (ou veio sem lote): lote automático
   NF<numero> (ex.: NF18400), sem validade. Antes travava 'falta lote/validade' em todo item.
 - Data de fabricação não informada: usa a EMISSÃO da NF (o Bling só exige uma data para criar o lote). Antes tinha que
   salvar uma por uma e cada salvamento batia no Bling.
Edita recebimento_routes.py no lugar (âncoras exatas). Backup .bak_loteauto_<ts>."""
import sys, time, shutil
p = sys.argv[1] if len(sys.argv) > 1 else "recebimento_routes.py"
s = open(p, encoding="utf-8").read()
if 'fab_origem = "emissao"' in s:
    print("já remendado"); sys.exit(0)
R = [
('''        if not fab and lote_it:
            if rastros is None:
                rastros = _rastros_xml(r["xml"] or "")
            fab, fab_origem = _fabricacao_auto(lote_it, rastros.get(int(it["n_item"] or 0)))
''', '''        if not fab and lote_it:
            if rastros is None:
                rastros = _rastros_xml(r["xml"] or "")
            fab, fab_origem = _fabricacao_auto(lote_it, rastros.get(int(it["n_item"] or 0)))
        # 27/11: sem lote (produto 'sem lote / não vence' ou lote não informado) -> lote automático NF<numero>, usado só se o Bling controlar lote
        lote_auto = False
        if not lote_it and not (it.get("serie") or "").strip():
            lote_it, lote_auto = f"NF{str(r['numero'] or '').strip() or r['id']}", True
        # 27/11: sem data de fabricação -> emissão da NF (o Bling só precisa de uma data para criar o lote)
        if not fab and (r["emissao"] or "").strip():
            fab, fab_origem = (r["emissao"] or "").strip()[:10], "emissao"
'''),
('''                               "serie": (it.get("serie") or "").strip(), "controla_lote": False,
                               "fabricacao": fab or "", "fabricacao_origem": fab_origem, "lote_existe": False})''',
 '''                               "serie": (it.get("serie") or "").strip(), "controla_lote": False, "lote_auto": lote_auto,
                               "fabricacao": fab or "", "fabricacao_origem": fab_origem, "lote_existe": False})'''),
('''                p["controla_lote"] = ctl.get(int(p["pid"]), False)
                if p["controla_lote"] and not (p["lote"] and (p["validade"] or p["validade_indet"])):
                    ponto(False, f"Item {p['n_item']}", f"{(p['descricao'] or '')[:40]}: o Bling controla lote desse produto e falta lote/validade")
                elif p["controla_lote"] and not p["fabricacao"]:''',
 '''                p["controla_lote"] = ctl.get(int(p["pid"]), False)
                if not p["controla_lote"] and p.get("lote_auto"):
                    p["lote"] = ""      # Bling não controla lote: não inventa lote
                if p["controla_lote"] and p.get("lote_auto"):
                    ponto(True, f"Item {p['n_item']}", f"{(p['descricao'] or '')[:40]}: sem lote informado — vai com o lote automático {p['lote']} (fabricação = emissão da NF)")
                if p["controla_lote"] and not p["lote"]:
                    ponto(False, f"Item {p['n_item']}", f"{(p['descricao'] or '')[:40]}: o Bling controla lote desse produto e falta lote/validade")
                elif p["controla_lote"] and not p["fabricacao"]:'''),
]
for a, b in R:
    if s.count(a) != 1:
        print(f"âncora não encontrada ({s.count(a)}x): {a[:70]!r}"); sys.exit(2)
for a, b in R:
    s = s.replace(a, b)
bak = f"{p}.bak_loteauto_{time.strftime('%Y%m%d%H%M%S')}"
shutil.copy2(p, bak); open(p, "w", encoding="utf-8").write(s); compile(s, p, "exec")
print(f"OK remendado. backup: {bak}")
