#!/usr/bin/env bash
# VOLTA o CODIGO da LHVMed para um backup feito por backup_wms_lhvmed.sh e reinicia o servico.
# Nao mexe no banco (os pedidos de hoje ficam). Uso (dentro da pasta do backend):
#   bash reverter_wms_lhvmed.sh ~/backups_wms/lhvmed_AAAAMMDD_HHMMSS
set -uo pipefail
B="${1:-}"
[ -n "$B" ] && [ -f "$B/codigo.tar.gz" ] || { echo "uso: bash reverter_wms_lhvmed.sh <pasta_do_backup>"; exit 1; }
[ -f app.py ] || { echo "ERRO: rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "ERRO: so LHVMed (pasta RussianL)"; exit 1;; esac

echo "Voltando o codigo para: $B"
tar -xzf "$B/codigo.tar.gz" -C "$(dirname "$PWD")" || { echo "FALHOU ao restaurar"; exit 1; }
rm -f fefo_routes.py fefo_sync.py app.py.bak_fefo        # modulos novos (se existirem) saem de cena
echo "Reiniciando wms-russia..."
sudo systemctl restart wms-russia.service && sleep 3 && systemctl is-active wms-russia.service
curl -s -o /dev/null -w "API local: HTTP %{http_code}\n" localhost:8000/api/mapa/status
echo "Pronto. (Banco NAO foi alterado. So em ultimo caso: pg_restore --clean --if-exists -d <url> $B/banco.dump — isso volta TODO o banco para a hora do backup.)"
