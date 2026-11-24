#!/usr/bin/env bash
# Devoluções: "Buscar essa venda no ML" (traz a devolução de UMA venda agora, sem orçamento nem cache) e não guarda a lista
# de reclamações fechadas pela metade quando o ML corta (429). Biel 23/11. Uso: bash ligar_dev_venda.sh <commit> [medicald]
set -e
REF="${1:?commit}"
if [ "${2:-}" = "medicald" ]; then L="$HOME/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix"; SVC=wms-europa.service; PORTA=8001; else L="$HOME/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix"; SVC=wms-russia.service; PORTA=8000; fi
cd "$L"
blob() { venv/bin/python -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\\0'%len(d)+d).hexdigest()[:12])" "$1"; }
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/devolucoes_routes.py" -o .dev_novo
venv/bin/python -m py_compile .dev_novo
A=$(blob devolucoes_routes.py)
[ "$A" = "$(blob .dev_novo)" ] && { rm -f .dev_novo; echo "já instalado"; exit 0; }
if [ "$A" != "6f3ff0a379b4" ] && [ "$A" != "32ef6c75849e" ] && [ "$A" != "06acb1818ae7" ] && [ "$A" != "e3b529558fa1" ] && [ "${FORCAR:-0}" != "1" ]; then echo "devolucoes_routes.py do servidor ($A) não é o esperado (6f3ff0a379b4): parei. Me mande esse código."; rm -f .dev_novo; exit 1; fi
BK="devolucoes_routes.py.bak_venda_$(date +%Y%m%d%H%M%S)"; cp -p devolucoes_routes.py "$BK"; mv .dev_novo devolucoes_routes.py
sudo systemctl restart "$SVC"; sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORTA/api/devolucoes/importar-ml-venda || true)
if ! systemctl is-active --quiet "$SVC" || [ "$code" != 401 ]; then cp -p "$BK" devolucoes_routes.py; sudo systemctl restart "$SVC"; echo "não subiu ($code): voltei"; exit 1; fi
echo "OK ($SVC no ar). Desfazer: cd $L && cp -p $BK devolucoes_routes.py && sudo systemctl restart $SVC"
