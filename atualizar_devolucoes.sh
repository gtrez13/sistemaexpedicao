#!/usr/bin/env bash
# Atualiza SÓ o devolucoes_routes.py. Backup + revert de 1 comando.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
BK="devolucoes_routes.py.bak_$(date +%Y%m%d%H%M%S)"
cp devolucoes_routes.py "$BK"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/devolucoes_routes.py" -o devolucoes_routes.py.novo
venv/bin/python -m py_compile devolucoes_routes.py.novo
mv devolucoes_routes.py.novo devolucoes_routes.py
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — /api/devolucoes/1/ml-revisao respondeu $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/devolucoes/1/ml-revisao) (401 = rota existe, pede login)"
echo "Para desfazer: cp $BK devolucoes_routes.py && sudo systemctl restart wms-russia.service"
