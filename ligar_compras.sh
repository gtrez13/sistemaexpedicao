#!/usr/bin/env bash
# "Não encontrei" -> avisa o comprador no StockMind (compras.lhvmed.com.br) os produtos zerados e sem estoque (Biel 18/11).
# Troca naoachado_routes.py, baixa naoachado_compras.py e cria o drop-in compras.conf (COMPRAS_API_URL + COMPRAS_TOKEN).
# Backup + desfazer; se o serviço não subir, volta sozinho. LHVMed.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
URL="${2:-https://api-compras.lhvmed.com.br}"
SVC=wms-russia.service
DROP=/etc/systemd/system/$SVC.d/compras.conf
BASE=a426634                                   # naoachado_routes.py instalado pelo ligar_nao_achado.sh
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao"
curl -fsSL "$G/$BASE/naoachado_routes.py" -o .base_na
curl -fsSL "$G/$REF/naoachado_routes.py" -o .ref_na
if ! cmp -s .base_na naoachado_routes.py && ! cmp -s .ref_na naoachado_routes.py; then
  echo "O naoachado_routes.py do servidor não é o que o ligar_nao_achado.sh instalou: parei sem mexer."
  echo "Diferença (para me mandar):"; diff .base_na naoachado_routes.py | head -40; echo "(fim da diferença)"
  rm -f .base_na .ref_na; exit 1
fi
TS=$(date +%Y%m%d%H%M%S); BKD="bak_compras_$TS"; mkdir "$BKD"
cp -p naoachado_routes.py "$BKD/"
[ -f "$DROP" ] && sudo cp -p "$DROP" "$BKD/compras.conf.antes"
venv/bin/python -m py_compile .ref_na
curl -fsSL "$G/$REF/naoachado_compras.py" -o naoachado_compras.py
mv .ref_na naoachado_routes.py; rm -f .base_na
# token: reaproveita o que já existir; senão gera um novo
TOKEN=$(sudo sed -n 's/^Environment=COMPRAS_TOKEN=//p' "$DROP" 2>/dev/null || true)
[ -n "$TOKEN" ] || TOKEN=$(venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))")
sudo mkdir -p "/etc/systemd/system/$SVC.d"
printf '[Service]\nEnvironment=COMPRAS_API_URL=%s\nEnvironment=COMPRAS_TOKEN=%s\n' "$URL" "$TOKEN" | sudo tee "$DROP" >/dev/null
sudo chmod 600 "$DROP"
sudo systemctl daemon-reload
sudo systemctl restart $SVC
sleep 4
if ! systemctl is-active --quiet $SVC; then
  cp -p "$BKD/naoachado_routes.py" .; rm -f naoachado_compras.py
  if [ -f "$BKD/compras.conf.antes" ]; then sudo cp -p "$BKD/compras.conf.antes" "$DROP"; else sudo rm -f "$DROP"; fi
  sudo systemctl daemon-reload; sudo systemctl restart $SVC
  echo "O serviço não subiu com o arquivo novo: voltei como estava. Nada mudou."; exit 1
fi
echo "OK: WMS no ar com o aviso ao comprador."
# testa o caminho até o StockMind sem criar nada (corpo vazio: 422 = token aceito e rota existe)
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' "$URL/wms/estoque-zerado" || echo 000)
case "$code" in
  422) echo "StockMind: pronto (token aceito). Os próximos 'Não encontrei' já aparecem para o comprador." ;;
  503) echo "StockMind: a rota existe, mas falta o WMS_TOKEN no .env do StockMind (veja abaixo)." ;;
  401) echo "StockMind: token diferente. O WMS_TOKEN do StockMind tem que ser igual ao de baixo." ;;
  404) echo "StockMind: ainda sem a atualização (rota /wms/estoque-zerado não existe lá)." ;;
  000) echo "StockMind: este servidor não alcançou $URL (rede/DNS). Até resolver, o aviso fica como erro e dá para reenviar depois." ;;
  *)   echo "StockMind respondeu HTTP $code." ;;
esac
echo
echo "No .env do backend do StockMind (o mesmo valor; depois reinicie o StockMind):"
echo "  WMS_TOKEN=$TOKEN"
echo
echo "Produtos zerados ANTES disso (prévia, nada é enviado):"
COMPRAS_API_URL="$URL" COMPRAS_TOKEN="$TOKEN" venv/bin/python -u naoachado_compras.py | sed 's/^/  /' || true
echo "Para mandar esses ao comprador (depois que o StockMind estiver pronto):"
echo "  COMPRAS_API_URL=$URL COMPRAS_TOKEN=\$(sudo sed -n 's/^Environment=COMPRAS_TOKEN=//p' $DROP) venv/bin/python -u naoachado_compras.py --enviar"
echo
echo "Desfazer:"
if [ -f "$BKD/compras.conf.antes" ]; then
  echo "  cp -p $BKD/naoachado_routes.py . && rm -f naoachado_compras.py && sudo cp -p $BKD/compras.conf.antes $DROP && sudo systemctl daemon-reload && sudo systemctl restart $SVC"
else
  echo "  cp -p $BKD/naoachado_routes.py . && rm -f naoachado_compras.py && sudo rm -f $DROP && sudo systemctl daemon-reload && sudo systemctl restart $SVC"
fi
