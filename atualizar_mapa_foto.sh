#!/usr/bin/env bash
# Atualiza SÓ o mapa_routes.py (foto via /api/mapa/foto/<sku>). Backup + revert de 1 comando.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
BK="mapa_routes.py.bak_foto_$(date +%Y%m%d%H%M)"
cp mapa_routes.py "$BK"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/mapa_routes.py" -o mapa_routes.py.novo
venv/bin/python -m py_compile mapa_routes.py.novo
mv mapa_routes.py.novo mapa_routes.py
rm -f fotos_cache/*.sem 2>/dev/null || true
sudo systemctl restart wms-russia.service
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/mapa/foto/TESTE)
echo "OK — /api/mapa/foto respondeu $code (401 = rota existe, pede login)"
echo "Reverter: cp $BK mapa_routes.py && sudo systemctl restart wms-russia.service"
