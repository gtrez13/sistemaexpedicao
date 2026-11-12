#!/usr/bin/env bash
# Chave do envio AUTOMÁTICO da nota de devolução para a SEFAZ.
# Uso: bash nf_dev_envio_auto.sh desligar   |   bash nf_dev_envio_auto.sh ligar
# Desligado: o WMS cria a nota, lança o estoque e para (supervisor clica "Enviar para a SEFAZ").
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
case "$1" in ligar) V=1;; desligar) V=0;; *) echo "use: bash nf_dev_envio_auto.sh ligar | desligar"; exit 1;; esac
BK=".env.bak_nfdevauto_$(date +%Y%m%d%H%M%S)"
cp .env "$BK"
sed -i '/^NF_DEV_ENVIAR_AUTO=/d' .env
printf '\nNF_DEV_ENVIAR_AUTO=%s\n' "$V" >> .env
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — envio automático para a SEFAZ: $([ $V = 1 ] && echo LIGADO || echo DESLIGADO) ($(systemctl is-active wms-russia.service))"
echo "Para desfazer: cp $BK .env && sudo systemctl restart wms-russia.service"
