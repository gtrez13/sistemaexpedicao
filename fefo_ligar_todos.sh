#!/usr/bin/env bash
# LIGA o FEFO para TODOS os SKUs que tem lote no Bling (LHVMed). Rode DENTRO da pasta RussianL, com o venv ativo.
# Antes: rode backup_wms_lhvmed.sh. Para desfazer: bash fefo_desligar.sh (1 comando, ~5 s).
# SKUs sem lote no Bling (ou com >=100 lotes) continuam sem FEFO: o sistema ignora sozinho.
set -uo pipefail
[ -f app.py ] || { echo "ERRO: rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "ERRO: so LHVMed (RussianL)"; exit 1;; esac
[ -f ~/backups_wms/.ok_hoje ] || echo "AVISO: confirme que fez o backup hoje (backup_wms_lhvmed.sh)."

echo "1/3 sincronizando lotes de todos os SKUs (repete ate acabar; para sozinho se der 429)..."
for i in $(seq 1 12); do
  out=$(python fefo_sync.py --todos --max 150 2>&1); rc=$?
  echo "$out" | tail -3
  echo "$out" | grep -q "^0 SKU(s)" && break
  [ $rc -eq 1 ] && { echo "PAROU (429/token). NAO liguei. Espere ~30 min e rode de novo."; exit 1; }
done

echo "2/3 ligando para todos (removendo a lista FEFO_SKUS do drop-in)..."
F=/etc/systemd/system/wms-russia.service.d/fefo.conf
sudo cp "$F" "$F.bak_$(date +%H%M%S)" 2>/dev/null
sudo sed -i '/FEFO_SKUS/d' "$F"
sudo systemctl daemon-reload && sudo systemctl restart wms-russia.service && sleep 3
systemctl is-active wms-russia.service

echo "3/3 conferindo:"; cat "$F"
echo "Pronto. Desligar: bash fefo_desligar.sh"
