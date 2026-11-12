#!/usr/bin/env bash
# Atualiza SÓ o recebimento_routes.py (Meu Danfe + casar NF x pedido de compra pelos itens). Backup + 1 comando para desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
BK="recebimento_routes.py.bak_$(date +%Y%m%d%H%M%S)"
cp recebimento_routes.py "$BK"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/recebimento_routes.py" -o recebimento_routes.py.novo
venv/bin/python -m py_compile recebimento_routes.py.novo
# robô da entrada (manifestar + importar a nota no Bling): arquivo novo, com backup se já existir
ROBO=services/bling_entrada_robo.py
[ -f "$ROBO" ] && cp "$ROBO" "$ROBO.bak_$(date +%Y%m%d%H%M%S)"
curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF/$ROBO" -o "$ROBO.novo"
venv/bin/python -m py_compile "$ROBO.novo"
mv "$ROBO.novo" "$ROBO"
mv recebimento_routes.py.novo recebimento_routes.py
grep -q "^MEUDANFE_TOKEN=" .env && echo "Meu Danfe: token ok no .env" || echo "ATENÇÃO: falta MEUDANFE_TOKEN no .env"
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — /api/recebimento/resumo respondeu $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/recebimento/resumo) (401 = rota existe, pede login)"
echo "Para desfazer: cp $BK recebimento_routes.py && sudo systemctl restart wms-russia.service"
