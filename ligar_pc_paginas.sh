#!/usr/bin/env bash
# Recebimento: pedidos de compra do fornecedor em todas as páginas (Bling ignora o filtro de situação e devolve os 50 mais
# antigos; DORJA x 3304, Biel 23/11). Uso: bash ligar_pc_paginas.sh <commit> [medicald]
set -e
REF="${1:?commit}"
if [ "${2:-}" = "medicald" ]; then L="$HOME/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix"; SVC=wms-europa.service; PORTA=8001; else L="$HOME/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix"; SVC=wms-russia.service; PORTA=8000; fi
cd "$L"
blob() { venv/bin/python -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\\0'%len(d)+d).hexdigest()[:12])" "$1"; }
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/recebimento_routes.py" -o .receb_novo
venv/bin/python -m py_compile .receb_novo
A=$(blob recebimento_routes.py)
[ "$A" = "$(blob .receb_novo)" ] && { rm -f .receb_novo; echo "já instalado"; exit 0; }
if [ "$A" != "890817eddabb" ] && [ "${FORCAR:-0}" != "1" ]; then echo "recebimento_routes.py do servidor ($A) não é o esperado (890817eddabb): parei"; rm -f .receb_novo; exit 1; fi
BK="recebimento_routes.py.bak_pc_$(date +%Y%m%d%H%M%S)"; cp -p recebimento_routes.py "$BK"; mv .receb_novo recebimento_routes.py
sudo systemctl restart "$SVC"; sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORTA/api/recebimento/resumo || true)
if ! systemctl is-active --quiet "$SVC" || [ "$code" != 401 ]; then cp -p "$BK" recebimento_routes.py; sudo systemctl restart "$SVC"; echo "não subiu ($code): voltei"; exit 1; fi
echo "OK ($SVC no ar). Na nota, clique 'Atualizar do Bling'. Desfazer: cd $L && cp -p $BK recebimento_routes.py && sudo systemctl restart $SVC"
