#!/usr/bin/env bash
# Entrada da NF 13003 (Biel 19/11 14:51), LHVMed. Uso:  bash ligar_conciliar.sh <commit>
# 1) Conciliação: o robô pegava um campo ESCONDIDO do Bling (#edChaveAcessoRefItem, "chave da nota referenciada") em vez da
#    busca da lista de Notas de compra e travava 20 s ("Locator.fill: Timeout 20000ms"). Agora pega só o campo que está na tela.
# 2) "Não achei a situação 'Atendido' dos pedidos de compra no Bling": procura no Bling (só lê) e grava o id no .env
#    (BLING_SITUACAO_PC_ATENDIDO_ID), para o WMS não depender dessa busca a cada entrada.
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
REF="${1:?passe o commit: bash ligar_conciliar.sh <commit>}"
L="$HOME/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix"
cd "$L" || { echo "não achei a pasta da LHVMed"; exit 1; }
BASES="849e2736e08c f46846f3e860 7a081477bd35 2f482c56b080"   # ef420c6 (busca da lista), 95c461e, b7149aa (login digitado), d3cbbe6
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
blob() { venv/bin/python -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\0'%len(d)+d).hexdigest()[:12])" "$1"; }

curl -fsSL "$G/services/bling_entrada_robo.py" -o .robo_novo
venv/bin/python -m py_compile .robo_novo
ATUAL=$(blob services/bling_entrada_robo.py); NOVO=$(blob .robo_novo)
TROCA=1
if [ "$ATUAL" = "$NOVO" ]; then
  TROCA=0; echo "robô: já é o novo"
elif ! echo " $BASES " | grep -q " $ATUAL " && [ "${FORCAR:-0}" != "1" ]; then
  echo "O services/bling_entrada_robo.py do servidor ($ATUAL) não é nenhum dos esperados: parei sem mexer."
  echo "Diferença para o b7149aa (para me mandar):"
  diff <(curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/b7149aa/services/bling_entrada_robo.py") services/bling_entrada_robo.py | head -40 || true
  rm -f .robo_novo; exit 1
fi

echo "Situação 'Atendido' dos pedidos de compra no Bling (só lê):"
OUT=$(venv/bin/python -W ignore - <<'PY'
import os, sys, unicodedata
from dotenv import load_dotenv
load_dotenv(".env")
sys.path.insert(0, os.getcwd())
from services.bling_service import BlingService
b = BlingService()
nz = lambda t: unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower().strip()
r = b._get("/situacoes/modulos")
mods = ((r.json() or {}).get("data") or []) if r.status_code == 200 else []
if not mods:
    print(f"  módulos: HTTP {r.status_code} {(r.text or '')[:300]}", file=sys.stderr)
else:
    print("  módulos: " + ", ".join(f"{m.get('id')}={m.get('nome')}" for m in mods), file=sys.stderr)
achou = []
for m in mods:
    if "compra" in nz(f"{m.get('nome')} {m.get('descricao')}"):
        r2 = b._get(f"/situacoes/modulos/{m['id']}")
        sits = ((r2.json() or {}).get("data") or []) if r2.status_code == 200 else []
        print(f"  {m.get('nome')} ({m['id']}): HTTP {r2.status_code} -> " + ", ".join(f"{s.get('id')}={s.get('nome')}" for s in sits), file=sys.stderr)
        achou += [s for s in sits if nz(s.get("nome")) in ("atendido", "atendida")]
if len(achou) == 1:
    print(f"SID={achou[0]['id']}")
PY
) || OUT=""
SID=$(printf '%s\n' "$OUT" | sed -n 's/^SID=\([0-9][0-9]*\)$/\1/p' | tail -1)
TS=$(date +%Y%m%d%H%M%S); BKD="bak_conciliar_$TS"; mkdir "$BKD"
cp -p services/bling_entrada_robo.py .env "$BKD/"
MUDOU_ENV=0
if grep -qE '^[[:space:]]*BLING_SITUACAO_PC_ATENDIDO_ID=' .env; then
  echo "  .env já tem BLING_SITUACAO_PC_ATENDIDO_ID: deixei como está"
elif [ -n "$SID" ]; then
  printf '\n# pedido de compra -> Atendido na entrada (ligar_conciliar.sh %s)\nBLING_SITUACAO_PC_ATENDIDO_ID=%s\n' "$TS" "$SID" >> .env
  MUDOU_ENV=1; echo "  achei: Atendido = $SID (gravado no .env)"
else
  echo "  !! não achei a situação Atendido sem dúvida: o .env ficou como estava (manda a lista acima para o Claude)"
fi
if [ "$TROCA" = 1 ]; then mv .robo_novo services/bling_entrada_robo.py; else rm -f .robo_novo; fi
if [ "$TROCA" = 0 ] && [ "$MUDOU_ENV" = 0 ]; then echo "Nada para mudar."; exit 0; fi
sudo systemctl restart wms-russia.service
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/auth/me || true)
if ! systemctl is-active --quiet wms-russia.service || ! { [ "$code" = 401 ] || [ "$code" = 200 ]; }; then
  cp -p "$BKD/bling_entrada_robo.py" services/; cp -p "$BKD/.env" .; sudo systemctl restart wms-russia.service
  echo "O serviço não subiu (auth/me = $code): voltei como estava. Nada mudou."; exit 1
fi
echo "OK: LHVMed no ar. Agora abra a NF 13003 no Recebimento e clique Continuar."
echo
echo "Desfazer:"
echo "  cd $L && cp -p $BKD/bling_entrada_robo.py services/ && cp -p $BKD/.env . && sudo systemctl restart wms-russia.service"
