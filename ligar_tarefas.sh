#!/usr/bin/env bash
# Tarefas + inventário rotativo (Biel 17/11), LHVMed: tarefas_routes.py (novo) + 8 linhas no fim do mapa_routes.py que ligam o módulo.
# Inventário: depois das 14h, seg-sex, o WMS monta a contagem do dia pelo mapa; o aviso só aparece na tela nova (Início).
# Ajustes no .env (opcional): INV_HORA=14:00  INV_DIAS=12345  INV_LIGADO=1  INV_BLING_ESCREVER=1  INV_AVISO_ADMIN=0
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
[ -f mapa_layout.py ] || { echo "Rode antes o ligar_dia.sh."; exit 1; }
REF="${1:?passe o commit}"
BASE=03d3d2c                          # mapa_routes.py do ligar_dia.sh (só para comparar)
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao"
TS=$(date +%Y%m%d%H%M%S); BKD="bak_tarefas_$TS"; mkdir "$BKD"
cp -p mapa_routes.py "$BKD/"; [ -f tarefas_routes.py ] && cp -p tarefas_routes.py "$BKD/"
TIRAR=""; [ -f tarefas_routes.py ] || TIRAR=" && rm -f tarefas_routes.py"
# o mapa_routes.py do servidor fica como está: só acrescento no fim as linhas que ligam o módulo novo
curl -fsSL "$G/$BASE/mapa_routes.py" -o .base
if ! cmp -s .base mapa_routes.py; then
  echo "Aviso: o mapa_routes.py do servidor é diferente do que o ligar_dia.sh baixou. Não troco ele; só acrescento as linhas do módulo."
  echo "Diferença (para me mandar):"; diff .base mapa_routes.py | head -40; echo "(fim da diferença)"
fi
rm -f .base
curl -fsSL "$G/$REF/tarefas_routes.py" -o tarefas_routes.py.novo
venv/bin/python -m py_compile tarefas_routes.py.novo
if ! grep -q "import tarefas_routes" mapa_routes.py; then
  cat >> mapa_routes.py <<'PYEOF'


# ───────────────────────── Tarefas + inventário rotativo (rotas em tarefas_routes.py) ─────────────────────────
try:
    import tarefas_routes as _tarefas
    _tarefas.registrar(mapa_routes)
except Exception as _e:                      # sem o arquivo novo: o resto segue no ar
    _slowlog.warning("[tarefas] módulo de tarefas não carregou: %s", _e)
PYEOF
fi
if ! venv/bin/python -m py_compile mapa_routes.py; then
  cp -p "$BKD/mapa_routes.py" .; rm -f tarefas_routes.py.novo
  echo "mapa_routes.py não compilou com as linhas novas: voltei como estava. Nada mudou."; exit 1
fi
mv tarefas_routes.py.novo tarefas_routes.py
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
