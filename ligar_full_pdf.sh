#!/usr/bin/env bash
# Full: PDF novo do mesmo frete (Biel 17/11: "o menino atualizou o full e a gente não pode perder"), LHVMed.
# Troca só o full_routes.py: rota /full/remessas/<id>/atualizar (compara o PDF novo com a remessa e grava sem perder
# o que já foi separado/etiquetado; a lista de antes fica em full_atualizacoes) e /devolver (sobra volta para a prateleira).
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
[ -f full_routes.py ] || { echo "Não achei o full_routes.py: rode antes o ligar_dia.sh."; exit 1; }
REF="${1:?passe o commit}"
BASE=bb721c4                          # full_routes.py que o ligar_dia.sh instalou
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao"
curl -fsSL "$G/$BASE/full_routes.py" -o .base_full
if ! cmp -s .base_full full_routes.py; then
  curl -fsSL "$G/$REF/full_routes.py" -o .ref_full
  if cmp -s .ref_full full_routes.py; then rm -f .base_full .ref_full; echo "Já está instalado ($REF). Nada mudou."; exit 0; fi
  echo "O full_routes.py do servidor não é o que o ligar_dia.sh instalou: parei sem mexer."
  echo "Diferença (para me mandar):"; diff .base_full full_routes.py | head -40; echo "(fim da diferença)"
  rm -f .base_full .ref_full; exit 1
fi
rm -f .base_full
TS=$(date +%Y%m%d%H%M%S); BKD="bak_fullpdf_$TS"; mkdir "$BKD"; cp -p full_routes.py "$BKD/"
curl -fsSL "$G/$REF/full_routes.py" -o full_routes.py.novo
venv/bin/python -m py_compile full_routes.py.novo
mv full_routes.py.novo full_routes.py
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp -p "$BKD/full_routes.py" .; sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com o arquivo novo: voltei como estava. Nada mudou."; exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8000/api/full/remessas/0/atualizar || true)
echo "OK: serviço no ar (/api/full/remessas/<id>/atualizar respondeu $code; 401 ou 302 = existe e pede login)"
echo
echo "Desfazer:"
echo "  cp -p $BKD/full_routes.py . && sudo systemctl restart wms-russia.service"
