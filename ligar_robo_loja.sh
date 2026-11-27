#!/usr/bin/env bash
# 27/11: robô de entrada — Notas recebidas pede "Selecione uma loja"; usar o campo de busca da lista (não a busca global).
# Troca só services/bling_entrada_robo.py (backup) e reinicia o serviço da pasta atual. Uso: bash ligar_robo_loja.sh <REF>
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
REF="${1:?passe o commit}"
case "$PWD" in *RussianL*) SVC=wms-russia.service;; *EuropaD*) SVC=wms-europa.service;; *) echo "pasta desconhecida"; exit 1;; esac
PY=venv/bin/python; [ -x "$PY" ] || PY=python3
TS=$(date +%Y%m%d%H%M%S)
cp -p services/bling_entrada_robo.py services/bling_entrada_robo.py.bak_loja_$TS
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/services/bling_entrada_robo.py" -o services/bling_entrada_robo.py.novo
"$PY" -m py_compile services/bling_entrada_robo.py.novo
mv services/bling_entrada_robo.py.novo services/bling_entrada_robo.py
sudo systemctl restart "$SVC"; sleep 3
systemctl is-active "$SVC" || { cp -p services/bling_entrada_robo.py.bak_loja_$TS services/bling_entrada_robo.py; sudo systemctl restart "$SVC"; echo "voltou o backup"; exit 1; }
echo "OK ($SVC no ar). Desfazer: cp -p services/bling_entrada_robo.py.bak_loja_$TS services/bling_entrada_robo.py && sudo systemctl restart $SVC"
