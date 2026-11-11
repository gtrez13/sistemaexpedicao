#!/usr/bin/env bash
# Comercial conferido na Expedição -> pedido no Bling vai para "Pronto para faturar". Backup + 1 comando para desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/comercial_faturar.py" -o comercial_faturar.py
venv/bin/python -m py_compile comercial_faturar.py
echo "== Situações de venda no Bling =="
venv/bin/python -u comercial_faturar.py --listar 2>/dev/null || true
venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import comercial_faturar as c; print('Pronto para faturar = id', c.situacao_faturar_id())" 2>&1 | tail -1
F=routes.py
BK="$F.bak_faturar_$(date +%Y%m%d%H%M%S)"
cp "$F" "$BK"
venv/bin/python - "$F" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
if "COMERCIAL_PRONTO_FATURAR" in s:
    print("já estava ligado"); sys.exit(0)
i = s.find('def scanner_bipar(')
j = s.find('if (row["origem"] or "ML") in ("COMERCIAL", "SHOPEE"):', i)
if i < 0 or j < 0:
    print("NÃO achei o fechamento do Comercial no scanner_bipar — nada mudou"); sys.exit(1)
m = re.compile(r'\n([ \t]+)return jsonify\(\{"success": True, "pedido_fechado": True, "faltando_itens": 0\}\)').search(s, j)
if not m or m.start() - j > 900:
    print("NÃO achei o return do fechamento do Comercial — nada mudou"); sys.exit(1)
ind = m.group(1)
novo = (f'\n{ind}# COMERCIAL_PRONTO_FATURAR: conferido -> Bling "Pronto para faturar" (só a situação)\n'
        f'{ind}_fat = None\n'
        f'{ind}if (row["origem"] or "") == "COMERCIAL":\n'
        f'{ind}    try:\n'
        f'{ind}        from comercial_faturar import marcar_pronto_para_faturar\n'
        f'{ind}        _fat = marcar_pronto_para_faturar(ml_id)\n'
        f'{ind}    except Exception as _e_fat:\n'
        f'{ind}        _fat = {{"ok": False, "erro": str(_e_fat)}}\n'
        f'{ind}return jsonify({{"success": True, "pedido_fechado": True, "faltando_itens": 0, "bling_faturar": _fat}})')
s = s[:m.start()] + novo + s[m.end():]
open(p, "w", encoding="utf-8").write(s)
print("ligado: Comercial conferido vai para Pronto para faturar")
PY
venv/bin/python -m py_compile "$F"
sudo systemctl restart wms-russia.service
sleep 3
echo "OK ($(systemctl is-active wms-russia.service)). Para passar os já conferidos: venv/bin/python -u comercial_faturar.py --aplicar"
echo "Para desfazer: cp $BK $F && sudo systemctl restart wms-russia.service"
