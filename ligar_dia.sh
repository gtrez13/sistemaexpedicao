#!/usr/bin/env bash
# Atualização de 17/11 (LHVMed):
#  1. Virada do dia confere a cada 30 min (antes: só 00:10), então o pedido que saiu no ML / foi cancelado / atendido no Bling
#     sai da Separação no mesmo dia.
#  2. Bip da Separação à prova de vários separadores ao mesmo tempo (2 bips no mesmo segundo não contam a mesma unidade e o
#     pedido fecha uma vez só). Testado com 50 separadores simultâneos.
#  3. Traz junto o mapa montável (mapa_layout.py) e o Full mais novo; sem mapa salvo, tudo igual a hoje.
# Backup de tudo numa pasta + desfazer (arquivos e crontab).
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
[ -f full_routes.py ] || { echo "Rode antes o ligar_full.sh."; exit 1; }
[ -f virada_dia.py ] || { echo "Rode antes o ligar_virada.sh."; exit 1; }
REF="${1:?passe o commit}"
BASE=94aff0a                          # versão de antes desta mudança (para não atropelar ajuste feito direto no servidor)
TS=$(date +%Y%m%d%H%M%S)
BKD="bak_dia_$TS"
URL="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
mkdir "$BKD"
TIRAR=""; [ -f mapa_layout.py ] || TIRAR=" && rm -f mapa_layout.py"
for f in mapa_routes.py mapa_layout.py full_routes.py full_auto.py virada_dia.py picking_routes.py recebimento_routes.py; do
  [ -f "$f" ] && cp -p "$f" "$BKD/"
done
crontab -l > "$BKD/crontab.txt" 2>/dev/null || true
for f in mapa_routes.py mapa_layout.py full_routes.py full_auto.py virada_dia.py picking_routes.py recebimento_routes.py; do
  curl -fsSL "$URL/$f" -o "$f.novo"
  venv/bin/python -m py_compile "$f.novo"
done

# picking e recebimento: só troca se o do servidor for o de sempre
AVISOS=""
for f in picking_routes.py recebimento_routes.py; do
  if curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$BASE/$f" -o .base 2>/dev/null && cmp -s .base "$f"; then
    mv "$f.novo" "$f"; AVISOS="$AVISOS\n$f: atualizado"
  else
    rm -f "$f.novo"; AVISOS="$AVISOS\n$f: NÃO trocado (o do servidor é diferente do esperado; me mande: md5sum $f)"
  fi
done
rm -f .base
for f in mapa_routes.py mapa_layout.py full_routes.py full_auto.py virada_dia.py; do mv "$f.novo" "$f"; done

sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp -p "$BKD"/*.py .
  [ -f "$BKD/mapa_layout.py" ] || rm -f mapa_layout.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com os arquivos novos: voltei tudo como estava. Nada mudou."
  exit 1
fi

# cron da virada: de 1x por dia (00:10) para a cada 30 min (hh:10 e hh:40)
DIR="$PWD"
( crontab -l 2>/dev/null | grep -v 'virada_dia_wms' ; \
  echo "10,40 * * * * cd $DIR && venv/bin/python -u virada_dia.py --cron >> virada_dia.log 2>&1 # virada_dia_wms" ) | crontab -
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/mapa/layout || true)
echo "OK: serviço no ar (/api/mapa/layout respondeu $code; 401 = existe, pede login)"
echo -e "${AVISOS#\\n}"
echo "Cron da virada agora:"
crontab -l | grep virada_dia_wms
echo "Rodando a virada agora (pode levar uns segundos):"
venv/bin/python -u virada_dia.py --cron || true
echo
echo "Desfazer:"
echo "  cp -p $BKD/*.py .$TIRAR && crontab $BKD/crontab.txt && sudo systemctl restart wms-russia.service"
