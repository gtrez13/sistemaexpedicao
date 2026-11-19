#!/usr/bin/env bash
# Inventário geral + pedido parado na Separação (Biel 19/11), LHVMed. Uso:  bash ligar_inventario_geral.sh <commit>
# Troca só o tarefas_routes.py: o galpão inteiro de uma vez (uma contagem por estrutura), conferido com o Bling produto por
# produto igual ao rotativo, e no fim o pop-up do que tem saldo no Bling e ninguém contou (Contar / Não encontrei = zera).
# Tabelas novas (inv_geral*) são criadas sozinhas, vazias. O rotativo das 14h só pausa enquanto um geral estiver aberto.
# Backup + desfazer; se o serviço não subir, volta sozinho. Não mexe na MedicalD.
set -e
REF="${1:?passe o commit: bash ligar_inventario_geral.sh <commit>}"
L="$HOME/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix"
cd "$L" || { echo "não achei a pasta da LHVMed"; exit 1; }
[ -f tarefas_routes.py ] || { echo "Não achei o tarefas_routes.py: rode antes o ligar_tarefas.sh."; exit 1; }
BASES_OK="57d0f96c1792 e567901b9b0f"   # tarefas_routes.py do ligar_inventario_geral (0f570c5) ou do ligar_tarefas
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
blob() { venv/bin/python -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\0'%len(d)+d).hexdigest()[:12])" "$1"; }
curl -fsSL "$G/tarefas_routes.py" -o .tarefas_novo
venv/bin/python -m py_compile .tarefas_novo
ATUAL=$(blob tarefas_routes.py); NOVO=$(blob .tarefas_novo)
if [ "$ATUAL" = "$NOVO" ]; then rm -f .tarefas_novo; echo "Já está instalado ($REF). Nada mudou."; exit 0; fi
if ! echo " $BASES_OK " | grep -q " $ATUAL " && [ "${FORCAR:-0}" != "1" ]; then
  echo "O tarefas_routes.py do servidor ($ATUAL) não é um dos esperados ($BASES_OK): parei sem mexer."
  echo "Diferença (para me mandar):"; diff <(curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/8ad1096/tarefas_routes.py") tarefas_routes.py | head -40 || true
  rm -f .tarefas_novo; exit 1
fi
TS=$(date +%Y%m%d%H%M%S); BKD="bak_invgeral_$TS"; mkdir "$BKD"; cp -p tarefas_routes.py "$BKD/"
mv .tarefas_novo tarefas_routes.py
sudo systemctl restart wms-russia.service
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/inventario/geral || true)
if ! systemctl is-active --quiet wms-russia.service || ! { [ "$code" = 401 ] || [ "$code" = 302 ] || [ "$code" = 200 ]; }; then
  cp -p "$BKD/tarefas_routes.py" .; sudo systemctl restart wms-russia.service
  echo "O serviço não subiu direito com o arquivo novo (/api/inventario/geral = $code): voltei como estava. Nada mudou."; exit 1
fi
echo "OK: LHVMed no ar. /api/inventario/geral respondeu $code (401 = existe e pede login)."
echo "Para usar: Inventário > Inventário geral > Começar (admin ou supervisor)."
echo
echo "Desfazer:"
echo "  cd $L && cp -p $BKD/tarefas_routes.py . && sudo systemctl restart wms-russia.service"
