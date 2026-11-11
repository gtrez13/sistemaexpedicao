#!/usr/bin/env bash
# Comercial (vendas internas do Bling): importa SÓ pedidos "Em andamento" (situação 15). Antes vinha "Atendido" (9) junto.
# Backup + 1 comando para desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
F=services/bling_service.py
BK="$F.bak_comercial_$(date +%Y%m%d%H%M%S)"
cp "$F" "$BK"
venv/bin/python - "$F" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
i = s.find("def buscar_vendas_internas")
if i < 0:
    sys.exit("NÃO achei buscar_vendas_internas — nada mudou")
j = s.find("\n    def ", i + 10); j = len(s) if j < 0 else j
f = s[i:j]
f2, n = re.subn(r'"idsSituacoes\[\]"\s*:\s*\[[^\]]*\]', '"idsSituacoes[]": [15]', f)
if n == 0:
    print("NÃO achei o filtro de situação. Linhas da função com 'situa':")
    for k, l in enumerate(f.splitlines()):
        if "itua" in l: print(k, l)
    sys.exit(1)
if "COMERCIAL_SO_EM_ANDAMENTO" not in f2:
    m = re.search(r'\n(\s*)if _ml_pat\.match\(numero_loja\):\n\s*continue\n', f2)
    if m:
        ind = m.group(1)
        guard = (f"{ind}# COMERCIAL_SO_EM_ANDAMENTO: garante só 'Em andamento' mesmo se o Bling ignorar o filtro\n"
                 f"{ind}_sit = (pv.get('situacao') or {{}}).get('id')\n"
                 f"{ind}if _sit is not None and str(_sit) != '15':\n{ind}    continue\n")
        f2 = f2[:m.end()] + guard + f2[m.end():]
open(p, "w", encoding="utf-8").write(s[:i] + f2 + s[j:])
print(f"filtro trocado para só 'Em andamento' ({n} lugar)")
PY
venv/bin/python -m py_compile "$F"
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — Comercial agora só puxa 'Em andamento' ($(systemctl is-active wms-russia.service))."
echo "Para desfazer: cp $BK $F && sudo systemctl restart wms-russia.service"
