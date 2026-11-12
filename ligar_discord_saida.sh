#!/usr/bin/env bash
# Liga o alerta da SAÍDA no Discord (lista fechada com pedido faltando: foto, justificativa e pedidos que ficaram).
# Uso: bash ligar_discord_saida.sh 'https://discord.com/api/webhooks/....'
# (Discord: configurações do canal > Integrações > Webhooks > Novo webhook > Copiar URL do webhook)
# Backup do .env + 1 comando para desfazer. Manda uma mensagem de teste no canal.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
URL="$1"
printf '%s' "$URL" | grep -qE '^https://(discord|discordapp)\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+$' \
  || { echo "isso não é uma URL de webhook do Discord (começa com https://discord.com/api/webhooks/...)"; exit 1; }
C=$(curl -s -o /tmp/discord_teste.txt -w '%{http_code}' -H 'Content-Type: application/json' \
  -d '{"content":"Teste do WMS LHVMed: os alertas da Saída (lista fechada com pedido faltando) vão chegar neste canal."}' "$URL" || true)
[ "$C" = "204" ] || [ "$C" = "200" ] || { echo "O Discord recusou (HTTP $C): $(head -c 300 /tmp/discord_teste.txt). Não gravei nada."; exit 1; }
BK=".env.bak_discord_$(date +%Y%m%d%H%M%S)"
cp .env "$BK"
sed -i '/^DISCORD_WEBHOOK_SAIDA=/d' .env
printf '\nDISCORD_WEBHOOK_SAIDA=%s\n' "$URL" >> .env
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — mensagem de teste enviada ao canal e alerta da Saída ligado ($(systemctl is-active wms-russia.service))."
echo "Para desfazer: cp $BK .env && sudo systemctl restart wms-russia.service"
