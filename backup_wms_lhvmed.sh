#!/usr/bin/env bash
# BACKUP recuperavel do WMS LHVMed (codigo + banco). Rode DENTRO da pasta do backend da LHVMed.
# Nao altera nada no sistema: so le e gera arquivos em ~/backups_wms/.
set -uo pipefail
[ -f app.py ] || { echo "ERRO: rode dentro da pasta do backend (onde esta o app.py)"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "ERRO: este script e SO da LHVMed (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

DEST="$HOME/backups_wms/lhvmed_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$DEST" && chmod 700 "$HOME/backups_wms" "$DEST"

echo "1/4 codigo (inclui .env e db/wms.db; sem venv/caches)..."
tar --exclude=venv --exclude=__pycache__ --exclude=node_modules --exclude=downloads \
    -czf "$DEST/codigo.tar.gz" -C "$(dirname "$PWD")" "$(basename "$PWD")" || { echo "FALHOU o tar"; exit 1; }

echo "2/4 banco Postgres..."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -n "${DATABASE_URL:-}" ]; then
  pg_dump "$DATABASE_URL" -Fc -f "$DEST/banco.dump" && echo "   banco.dump ok" || { echo "FALHOU o pg_dump"; exit 1; }
else
  echo "   DATABASE_URL nao encontrado no .env — banco NAO foi copiado (confira!)"
fi

echo "3/4 servico, cron e versao..."
systemctl cat wms-russia.service > "$DEST/wms-russia.service.txt" 2>/dev/null
crontab -l > "$DEST/crontab.txt" 2>/dev/null
{ git rev-parse HEAD; git status --short; } > "$DEST/git.txt" 2>&1
echo "$PWD" > "$DEST/pasta_origem.txt"

echo "4/4 permissoes..."
chmod 600 "$DEST"/*
echo; echo "BACKUP PRONTO: $DEST"; ls -lh "$DEST"
echo; echo "Para voltar atras (so codigo, 1 comando):  bash reverter_wms_lhvmed.sh \"$DEST\""
