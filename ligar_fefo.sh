#!/usr/bin/env bash
# FEFO na separação para TODOS os produtos com lote (Biel 13/11):
#   bipou um lote (etiqueta SKU*LOTE do WMS ou do fabricante) e existe lote com validade MAIS ANTIGA em estoque
#   -> a tela trava e manda pegar o mais antigo (supervisor pode liberar). Bip só do SKU passa e a tela já mostra
#   qual lote pegar (FEFO_MODO=lote). Lote que o sistema ainda não conhece não trava ninguém.
# Faz: atualiza fefo_routes.py / fefo_sync.py / recebimento_routes.py (backup), sincroniza os lotes dos SKUs dos
#      pedidos abertos, liga para todos (tira FEFO_SKUS), agenda a sincronização de hora em hora (cron).
# Uso: bash ligar_fefo.sh <REF>        Desligar (bipa como antes): bash fefo_desligar.sh
set -uo pipefail
REF="${1:-}"; [ -n "$REF" ] || { echo "uso: bash ligar_fefo.sh <REF>"; exit 1; }
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
grep -q "fefo_routes" app.py || { echo "o app.py não registra o FEFO: rode antes instalar_fefo.sh"; exit 1; }
TS=$(date +%Y%m%d%H%M%S)
URL="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
F=/etc/systemd/system/wms-russia.service.d/fefo.conf
ARQS="fefo_routes.py fefo_sync.py recebimento_routes.py"

voltar() {
  for a in $ARQS; do [ -f "$a.bak_$TS" ] && cp "$a.bak_$TS" "$a"; done
  [ -f "$F.bak_$TS" ] && sudo cp "$F.bak_$TS" "$F"
  sudo systemctl daemon-reload; sudo systemctl restart wms-russia.service
}

echo "1/5 baixando os arquivos ($REF)..."
for a in $ARQS; do
  cp "$a" "$a.bak_$TS"
  if ! curl -fsSL "$URL/$a" -o "$a.novo" || ! venv/bin/python -m py_compile "$a.novo"; then
    rm -f "$a.novo"; voltar; echo "não consegui baixar $a: voltei tudo como estava"; exit 1
  fi
  mv "$a.novo" "$a"
done

echo "2/5 sincronizando os lotes dos produtos dos pedidos abertos (lê o Bling devagar)..."
venv/bin/python -u fefo_sync.py --pendentes --max 600 2>&1 | tail -4
[ "${PIPESTATUS[0]}" = "1" ] && echo "   (parou no limite do Bling: o resto entra na sincronização de hora em hora; produto sem lote sincronizado bipa como hoje)"

echo "3/5 ligando para todos os produtos (FEFO_MODO=lote)..."
sudo mkdir -p "$(dirname "$F")"
if [ -f "$F" ]; then sudo cp "$F" "$F.bak_$TS"; else printf '[Service]\nEnvironment="FEFO_ATIVO=1"\n' | sudo tee "$F" >/dev/null; fi
sudo sed -i -E '/FEFO_SKUS|FEFO_MODO/d' "$F"
grep -q "FEFO_ATIVO" "$F" || echo 'Environment="FEFO_ATIVO=1"' | sudo tee -a "$F" >/dev/null
echo 'Environment="FEFO_MODO=lote"' | sudo tee -a "$F" >/dev/null
sudo systemctl daemon-reload && sudo systemctl restart wms-russia.service && sleep 5
if ! systemctl is-active --quiet wms-russia.service; then voltar; echo "o serviço não subiu: voltei tudo como estava"; exit 1; fi
systemctl show -p Environment wms-russia.service | grep -q "FEFO_SKUS" && echo "   ATENÇÃO: FEFO_SKUS ainda está em outro arquivo do serviço (systemctl cat wms-russia.service)"

echo "4/5 agendando a sincronização de hora em hora..."
crontab -l > "crontab.bak_$TS" 2>/dev/null || true
if crontab -l 2>/dev/null | grep -q "fefo_sync.py"; then
  echo "   já existia uma sincronização no cron: não mexi"
else
  ( crontab -l 2>/dev/null; echo "23 * * * * cd $PWD && venv/bin/python fefo_sync.py --todos --max 80 >> $PWD/fefo_sync.log 2>&1 # fefo_sync_wms" ) | crontab -
  echo "   ok: todo hh:23 (log em fefo_sync.log)"
fi

echo "5/5 conferindo:"
cat "$F"
venv/bin/python - <<'PY'
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from db import db_session
with db_session() as c:
    a = c.execute("SELECT COUNT(*) AS n FROM fefo_sku").fetchone()["n"]
    b = c.execute("SELECT COUNT(*) AS n FROM fefo_sku WHERE n_lotes>0").fetchone()["n"]
    l = c.execute("SELECT COUNT(*) AS n FROM fefo_lote WHERE saldo>0").fetchone()["n"]
print(f"   SKUs sincronizados: {a} · com lote: {b} · lotes com saldo: {l}")
PY
echo "Pronto. FEFO ligado para todos os produtos com lote."
echo "Desligar o FEFO (bipa como antes): bash fefo_desligar.sh"
echo "Voltar tudo como estava: for a in $ARQS; do cp \$a.bak_$TS \$a; done; sudo cp $F.bak_$TS $F; crontab crontab.bak_$TS; sudo systemctl daemon-reload; sudo systemctl restart wms-russia.service"
