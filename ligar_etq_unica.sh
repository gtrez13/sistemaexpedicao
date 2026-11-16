#!/usr/bin/env bash
# Recebimento: etiqueta com código único por unidade; a conferência é bipar cada etiqueta (cada código conta uma vez).
# Atualiza recebimento_routes.py. Backup + desfazer. Seguro com a tela antiga: só vale na nota que tiver etiqueta com código.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
BK="recebimento_routes.py.bak_etq_$(date +%Y%m%d%H%M%S)"
cp recebimento_routes.py "$BK"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/recebimento_routes.py" -o recebimento_routes.py.novo
venv/bin/python -m py_compile recebimento_routes.py.novo
mv recebimento_routes.py.novo recebimento_routes.py
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp "$BK" recebimento_routes.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com o arquivo novo: voltei o recebimento_routes.py de antes. Nada mudou."
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/api/recebimento/notas/0/etiquetas/codigos)
echo "OK: serviço no ar, rota das etiquetas respondeu $code (401 = existe, pede login)"
echo
echo "Desfazer:"
echo "  cp $BK recebimento_routes.py && sudo systemctl restart wms-russia.service"
