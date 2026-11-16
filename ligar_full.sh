#!/usr/bin/env bash
# Módulo FULL: remessas para o Mercado Livre Full nascem no WMS (o ML não deixa listar as do painel), aviso com som em todo WMS aberto,
# separação com bip e dados da etiqueta do ML (código do anúncio no Full). Atualiza mapa_routes.py + full_routes.py. Backup + desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
[ -f virada_dia.py ] || { echo "Rode antes: bash ligar_virada.sh 1ff8f20 (o mapa_routes.py novo já traz a virada)."; exit 1; }
REF="${1:?passe o commit}"
TS=$(date +%Y%m%d%H%M%S)
BK="mapa_routes.py.bak_full_$TS"
URL="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
cp mapa_routes.py "$BK"
for f in mapa_routes.py full_routes.py; do curl -fsSL "$URL/$f" -o "$f.novo"; done
venv/bin/python -m py_compile mapa_routes.py.novo full_routes.py.novo
for f in mapa_routes.py full_routes.py; do mv "$f.novo" "$f"; done
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp "$BK" mapa_routes.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com o arquivo novo: voltei o mapa_routes.py de antes. Nada mudou."
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/full/novas)
echo "OK: serviço no ar, /api/full/novas respondeu $code (401 = existe, pede login)"
echo "Lendo os anúncios Full do ML (só leitura)..."
venv/bin/python -u - <<'PY' || echo "(leitura falhou: manda o erro acima; a tela tenta de novo sozinha)"
import full_routes as F
F._atualizar_anuncios()
print("anúncios Full guardados:", F._anuncios["n"], "| erro:", F._anuncios["erro"])
PY
echo
echo "Desfazer:"
echo "  cp $BK mapa_routes.py && sudo systemctl restart wms-russia.service"
