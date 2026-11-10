#!/usr/bin/env bash
# Liga o WMS (devoluções) ao SAC: acha a porta do SAC nesta máquina e grava no .env do WMS.
# Backup do .env + 1 comando para desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac

CAND=""
for d in /var/www/sac-lhvmed /var/www/sac-lhv "$HOME/sac-lhv"; do
  [ -f "$d/.env" ] && CAND="$CAND $(grep -E '^PORT=' "$d/.env" 2>/dev/null | head -1 | cut -d= -f2 | tr -dc '0-9')"
done
CAND="$CAND $(ss -ltnp 2>/dev/null | grep -i node | grep -oE ':[0-9]{4,5} ' | tr -d ': ' | sort -u | tr '\n' ' ') 3001 3000 3002"

URL=""
for p in $CAND; do
  [ -n "$p" ] || continue
  c=$(curl -s -o /tmp/sac_teste.json -w '%{http_code}' "http://127.0.0.1:$p/api/service-orders?limit=1" || true)
  if [ "$c" = "200" ] && head -c 1 /tmp/sac_teste.json | grep -qE '[\[{]'; then URL="http://127.0.0.1:$p"; break; fi
done
[ -n "$URL" ] || { echo "NÃO achei o SAC rodando nesta máquina (testei as portas:$CAND). Me mande: ss -ltnp | grep node"; exit 1; }
echo "SAC encontrado em $URL"

BK=".env.bak_sac_$(date +%Y%m%d%H%M%S)"
cp .env "$BK"
sed -i '/^SAC_API_URL=/d;/^SAC_DEVOLUCAO_PATH=/d' .env
printf '\nSAC_API_URL=%s\nSAC_DEVOLUCAO_PATH=/api/service-orders\n' "$URL" >> .env
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — WMS ligado ao SAC ($(systemctl is-active wms-russia.service)). Devolução que chegar com problema abre O.S. sozinha."
echo "Para desfazer: cp $BK .env && sudo systemctl restart wms-russia.service"
