#!/usr/bin/env bash
# E-mail e telefone que a nota de devolução usa quando o cliente não tem no Bling nem no ML.
# Uso: bash nf_dev_padrao.sh EMAIL TELEFONE     (backup do .env + 1 comando para desfazer)
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
EM="$1"; TEL="$(printf '%s' "$2" | tr -dc '0-9')"
printf '%s' "$EM" | grep -qE '^[^@ ]+@[^@ ]+\.[^@ ]+$' || { echo "e-mail inválido: '$EM'"; exit 1; }
[ ${#TEL} -ge 10 ] && [ ${#TEL} -le 11 ] || { echo "telefone inválido: '$2' (use DDD + número, ex.: 11999998888)"; exit 1; }
BK=".env.bak_nfdev_$(date +%Y%m%d%H%M%S)"
cp .env "$BK"
sed -i '/^NF_DEV_EMAIL_PADRAO=/d;/^NF_DEV_TELEFONE_PADRAO=/d' .env
printf '\nNF_DEV_EMAIL_PADRAO=%s\nNF_DEV_TELEFONE_PADRAO=%s\n' "$EM" "$TEL" >> .env
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — padrão gravado: $EM / $TEL ($(systemctl is-active wms-russia.service))"
echo "Para desfazer: cp $BK .env && sudo systemctl restart wms-russia.service"
