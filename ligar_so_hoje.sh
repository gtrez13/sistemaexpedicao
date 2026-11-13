#!/usr/bin/env bash
# Separação só com o que sai HOJE: ML de outro dia sai da lista e volta no dia dele; Comercial só "Em andamento" no Bling.
# Atualiza mapa_routes.py + separacao_so_hoje.py. Backup + desfazer em 2 comandos.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
BK="mapa_routes.py.bak_sohoje_$(date +%Y%m%d%H%M%S)"
URL="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
cp mapa_routes.py "$BK"
curl -fsSL "$URL/mapa_routes.py" -o mapa_routes.py.novo
curl -fsSL "$URL/separacao_so_hoje.py" -o separacao_so_hoje.py.novo
venv/bin/python -m py_compile mapa_routes.py.novo separacao_so_hoje.py.novo
mv mapa_routes.py.novo mapa_routes.py
mv separacao_so_hoje.py.novo separacao_so_hoje.py
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp "$BK" mapa_routes.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com o arquivo novo: voltei o mapa_routes.py de antes. Nada mudou."
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/separacao/so-hoje)
echo "OK: serviço no ar, /api/separacao/so-hoje respondeu $code (401 = rota existe, pede login)"
echo
echo "Prévia (só lê o ML e o Bling, NÃO muda nada):"
venv/bin/python -u separacao_so_hoje.py --previa || echo "(prévia falhou: manda o erro acima)"
echo
echo "A tela aplica sozinha ao abrir a Separação e no Atualizar pedidos."
echo "Desfazer:"
echo "  venv/bin/python -u separacao_so_hoje.py --devolver"
echo "  cp $BK mapa_routes.py && sudo systemctl restart wms-russia.service"
