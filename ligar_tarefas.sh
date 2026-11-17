#!/usr/bin/env bash
# Tarefas + inventário rotativo (Biel 17/11), LHVMed: tarefas_routes.py (novo) + mapa_routes.py (só registra o módulo novo).
# Inventário: depois das 14h, seg-sex, o WMS monta a contagem do dia pelo mapa; o aviso só aparece na tela nova (Início).
# Ajustes no .env (opcional): INV_HORA=14:00  INV_DIAS=12345  INV_LIGADO=1  INV_BLING_ESCREVER=1  INV_AVISO_ADMIN=0
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
[ -f mapa_layout.py ] || { echo "Rode antes o ligar_dia.sh."; exit 1; }
REF="${1:?passe o commit}"
BASE=03d3d2c                          # mapa_routes.py instalado pelo ligar_dia.sh
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao"
TS=$(date +%Y%m%d%H%M%S); BKD="bak_tarefas_$TS"; mkdir "$BKD"
cp -p mapa_routes.py "$BKD/"; [ -f tarefas_routes.py ] && cp -p tarefas_routes.py "$BKD/"
TIRAR=""; [ -f tarefas_routes.py ] || TIRAR=" && rm -f tarefas_routes.py"
curl -fsSL "$G/$BASE/mapa_routes.py" -o .base
if ! cmp -s .base mapa_routes.py; then
  rm -f .base; rmdir "$BKD" 2>/dev/null || true
  echo "O mapa_routes.py do servidor não é o do ligar_dia.sh: parei sem mexer. Me mande: md5sum mapa_routes.py"; exit 1
fi
rm -f .base
for f in mapa_routes.py tarefas_routes.py; do curl -fsSL "$G/$REF/$f" -o "$f.novo"; venv/bin/python -m py_compile "$f.novo"; done
mv mapa_routes.py.novo mapa_routes.py; mv tarefas_routes.py.novo tarefas_routes.py
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp -p "$BKD"/*.py .; [ -f "$BKD/tarefas_routes.py" ] || rm -f tarefas_routes.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com os arquivos novos: voltei tudo como estava. Nada mudou."; exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/tarefas/aviso || true)
echo "OK: serviço no ar (/api/tarefas/aviso respondeu $code; 401 = existe, pede login)"
venv/bin/python - <<'PY' || true
import tarefas_routes as T
T._tabelas()
from db import db_session
with db_session() as c:
    p = T._plano(c)
print(f"Inventário de {p['mes']}: {p['total']} posições no mapa, {p['dias_uteis_restantes']} dias úteis até o fim do mês -> {p['cota_hoje']} posições por dia.")
print(f"A contagem de hoje sai às {p['hora']} (dia útil: {'sim' if p['dia_util'] else 'não'}).")
PY
echo
echo "Desfazer:"
echo "  cp -p $BKD/*.py .$TIRAR && sudo systemctl restart wms-russia.service"
