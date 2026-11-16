#!/usr/bin/env bash
# "Não encontrei": zera o estoque do produto no Bling (depósito Local) NA HORA do relato e avisa o supervisor/admin na tela;
# "Achei" devolve o estoque. Atualiza naoachado_routes.py e liga NAO_ACHADO_ZERAR_BLING=1. Backup + desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
SVC=wms-russia.service
DROP=/etc/systemd/system/$SVC.d/nao_achado.conf
BK="naoachado_routes.py.bak_na_$(date +%Y%m%d%H%M%S)"
cp naoachado_routes.py "$BK"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/naoachado_routes.py" -o naoachado_routes.py.novo
venv/bin/python -m py_compile naoachado_routes.py.novo
mv naoachado_routes.py.novo naoachado_routes.py
sudo mkdir -p "/etc/systemd/system/$SVC.d"
printf '[Service]\nEnvironment=NAO_ACHADO_ZERAR_BLING=1\nEnvironment=NAO_ACHADO_ZERAR_QUANDO=relato\n' | sudo tee "$DROP" >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart $SVC
sleep 4
if ! systemctl is-active --quiet $SVC; then
  cp "$BK" naoachado_routes.py; sudo rm -f "$DROP"; sudo systemctl daemon-reload; sudo systemctl restart $SVC
  echo "O serviço não subiu com o arquivo novo: voltei como estava. Nada mudou."
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/api/nao-achado/0/achei)
echo "OK: serviço no ar, rota do Achei respondeu $code (401 = existe, pede login)"
echo
echo "Desfazer:"
echo "  sudo rm -f $DROP && sudo systemctl daemon-reload"
echo "  cp $BK naoachado_routes.py && sudo systemctl restart $SVC"
