#!/usr/bin/env bash
# Chave da ENTRADA AUTOMÁTICA no Bling quando a nota de compra é conferida no Recebimento.
# Uso: bash receb_entrada_auto.sh desligar   |   bash receb_entrada_auto.sh ligar
# Desligado: o supervisor clica "Dar entrada no Bling" na nota conferida.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
case "$1" in ligar) V=1;; desligar) V=0;; *) echo "use: bash receb_entrada_auto.sh ligar | desligar"; exit 1;; esac
BK=".env.bak_recebauto_$(date +%Y%m%d%H%M%S)"
cp .env "$BK"
sed -i '/^RECEB_ENTRADA_AUTO=/d' .env
printf '\nRECEB_ENTRADA_AUTO=%s\n' "$V" >> .env
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — entrada automática no Bling ao conferir: $([ $V = 1 ] && echo LIGADO || echo DESLIGADO) ($(systemctl is-active wms-russia.service))"
echo "Para desfazer: cp $BK .env && sudo systemctl restart wms-russia.service"
