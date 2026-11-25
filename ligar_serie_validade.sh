#!/usr/bin/env bash
# Recebimento: nº de série (produto seriado, no lugar do lote) e "validade indeterminada" (Biel 25/11).
# Uso: bash ligar_serie_validade.sh <commit> [medicald]   (reversível: backup + comando de desfazer no fim)
set -e
REF="${1:?commit}"
if [ "${2:-}" = "medicald" ]; then L="$HOME/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix"; SVC=wms-europa.service; PORTA=8001; else L="$HOME/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix"; SVC=wms-russia.service; PORTA=8000; fi
cd "$L"
blob() { venv/bin/python -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\\0'%len(d)+d).hexdigest()[:12])" "$1"; }
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/recebimento_routes.py" -o .rec_novo
venv/bin/python -m py_compile .rec_novo
A=$(blob recebimento_routes.py)
[ "$A" = "$(blob .rec_novo)" ] && { rm -f .rec_novo; echo "já instalado"; exit 0; }
case " 6a28316f8507 6a28316f8507 e69de29bb2d1 890817eddabb 6e0f9a849843 " in *" $A "*) ;; *) if [ "${FORCAR:-0}" != "1" ]; then echo "recebimento_routes.py do servidor ($A) não é uma base conhecida: parei. Me mande esse código."; rm -f .rec_novo; exit 1; fi;; esac
BK="recebimento_routes.py.bak_serie_$(date +%Y%m%d%H%M%S)"; cp -p recebimento_routes.py "$BK"; mv .rec_novo recebimento_routes.py
sudo systemctl restart "$SVC"; sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORTA/api/recebimento/notas || true)
if ! systemctl is-active --quiet "$SVC" || [ "$code" != 401 ]; then cp -p "$BK" recebimento_routes.py; sudo systemctl restart "$SVC"; echo "não subiu ($code): voltei"; exit 1; fi
echo "OK ($SVC no ar). Desfazer: cd $L && cp -p $BK recebimento_routes.py && sudo systemctl restart $SVC"
