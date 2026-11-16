#!/usr/bin/env bash
# Virada do dia: o que é de dias anteriores e já terminou vira ARQUIVADO sozinho (nada é apagado; fica registrado e dá para voltar).
# Também traz a Separação só com o que sai hoje e o Arquivar da Expedição. Atualiza mapa_routes.py + separacao_so_hoje.py + virada_dia.py,
# põe o cron 00:10 (horário de Brasília). Backup + desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
TS=$(date +%Y%m%d%H%M%S)
BK="mapa_routes.py.bak_virada_$TS"
URL="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
cp mapa_routes.py "$BK"
for f in mapa_routes.py separacao_so_hoje.py virada_dia.py; do curl -fsSL "$URL/$f" -o "$f.novo"; done
venv/bin/python -m py_compile mapa_routes.py.novo separacao_so_hoje.py.novo virada_dia.py.novo
for f in mapa_routes.py separacao_so_hoje.py virada_dia.py; do mv "$f.novo" "$f"; done
echo "Prévia da virada (só lê, NÃO muda nada):"
venv/bin/python -u virada_dia.py --previa || echo "(prévia falhou: manda o erro acima)"
echo
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp "$BK" mapa_routes.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com o arquivo novo: voltei o mapa_routes.py de antes. Nada mudou."
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/arquivados)
echo "OK: serviço no ar, /api/arquivados respondeu $code (401 = existe, pede login)"
# cron 00:10 de Brasília no relógio do servidor
OFF=$(date +%z); SIG=${OFF:0:1}; HH=$((10#${OFF:1:2})); [ "$SIG" = "-" ] && HH=$((-HH))
H=$(( (3 + HH + 24) % 24 ))
crontab -l 2>/dev/null > "crontab.bak_virada_$TS" || true
( grep -v "# virada_dia_wms" "crontab.bak_virada_$TS" || true; echo "10 $H * * * cd $PWD && venv/bin/python -u virada_dia.py --cron >> virada_dia.log 2>&1 # virada_dia_wms" ) | crontab -
echo "Cron: todo dia $H:10 no relógio do servidor (= 00:10 em Brasília). Se não rodar, roda sozinho na 1ª vez que abrirem a Separação."
echo
echo "Desfazer (3 comandos):"
echo "  venv/bin/python -u virada_dia.py --desfazer \$(date +%F)"
echo "  crontab crontab.bak_virada_$TS"
echo "  cp $BK mapa_routes.py && sudo systemctl restart wms-russia.service"
