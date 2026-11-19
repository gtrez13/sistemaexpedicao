#!/usr/bin/env bash
# Entrada de NF na MedicalD igual à da LHVMed (Biel 19/11 15:41: "passa a mesma config para a MedicalD ... tenho que colocar
# a senha e login do Bling lá também"). Uso:  bash ligar_entrada_medicald.sh <commit>
# Só mexe na MedicalD (EuropaD, wms-europa, porta 8001):
#   1. robô da entrada e recebimento_routes.py na versão que fechou a NF 13003 na LHVMed (confere a versão que está lá)
#   2. pede o login e a senha do Bling DA MEDICALD aqui no terminal (a senha não aparece nem fica no histórico) e grava no .env
#   3. acha no Bling da MedicalD a situação "Atendido" dos pedidos de compra e grava BLING_SITUACAO_PC_ATENDIDO_ID
#   4. confere se o Playwright/Chromium do robô funcionam na pasta dela
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
REF="${1:?passe o commit: bash ligar_entrada_medicald.sh <commit>}"
M="$HOME/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix"
SVC=wms-europa.service
cd "$M" || { echo "não achei a pasta da MedicalD"; exit 1; }
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
blob() { venv/bin/python -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\0'%len(d)+d).hexdigest()[:12])" "$1"; }
ROBO_BASES="7a081477bd35 2f482c56b080 f46846f3e860 849e2736e08c 8d807a27007c"   # o que o ligar_medicald.sh pôs lá (e versões de hoje)
RECEB_BASES="1a704f35ffb6"

curl -fsSL "$G/services/bling_entrada_robo.py" -o .robo_novo
curl -fsSL "$G/recebimento_routes.py" -o .receb_novo
venv/bin/python -m py_compile .robo_novo .receb_novo
[ -f services/bling_entrada_robo.py ] && [ -f recebimento_routes.py ] || { echo "a MedicalD ainda não tem o recebimento: rode antes o ligar_medicald.sh"; exit 1; }
T_ROBO=0; T_RECEB=0
A=$(blob services/bling_entrada_robo.py)
if [ "$A" = "$(blob .robo_novo)" ]; then echo "robô: já é o novo"
elif echo " $ROBO_BASES " | grep -q " $A " || [ "${FORCAR:-0}" = "1" ]; then T_ROBO=1
else echo "o robô da MedicalD ($A) não é nenhuma versão conhecida: parei sem mexer (FORCAR=1 troca)"; rm -f .robo_novo .receb_novo; exit 1; fi
A=$(blob recebimento_routes.py)
if [ "$A" = "$(blob .receb_novo)" ]; then echo "recebimento_routes.py: já é o novo"
elif echo " $RECEB_BASES " | grep -q " $A " || [ "${FORCAR:-0}" = "1" ]; then T_RECEB=1
else echo "o recebimento_routes.py da MedicalD ($A) não é o conhecido: parei sem mexer (FORCAR=1 troca)"; rm -f .robo_novo .receb_novo; exit 1; fi

echo
echo "Login do Bling DA MEDICALD (o usuário que entra em bling.com.br; a senha não aparece ao digitar):"
read -r -p "  usuário/e-mail: " BUSER
read -r -s -p "  senha: " BPASS; echo
read -r -s -p "  senha de novo: " BPASS2; echo
[ -n "$BUSER" ] && [ -n "$BPASS" ] || { echo "usuário ou senha vazios: parei sem mexer"; rm -f .robo_novo .receb_novo; exit 1; }
[ "$BPASS" = "$BPASS2" ] || { echo "as senhas não batem: parei sem mexer"; rm -f .robo_novo .receb_novo; exit 1; }

TS=$(date +%Y%m%d%H%M%S); BKD="bak_entrada_$TS"; mkdir "$BKD"; chmod 700 "$BKD"
cp -p services/bling_entrada_robo.py recebimento_routes.py .env "$BKD/"
cat > "$BKD/desfazer.sh" <<EOF
#!/usr/bin/env bash
cd "$M" && cp -p "$BKD/bling_entrada_robo.py" services/ && cp -p "$BKD/recebimento_routes.py" "$BKD/.env" . && rm -f bling_state.json && sudo systemctl restart $SVC && echo "MedicalD de volta como antes: \$(systemctl is-active $SVC)"
EOF
chmod +x "$BKD/desfazer.sh"

# .env: login/senha (troca a linha se já existir) + situação Atendido
BUSER="$BUSER" BPASS="$BPASS" venv/bin/python - <<'PY'
import os, re
s = open(".env", encoding="utf-8").read()
def put(k, v):
    global s
    linha = f'{k}="{v}"'
    if re.search(rf"^\s*{k}\s*=", s, re.M):
        s = re.sub(rf"^\s*{k}\s*=.*$", linha, s, flags=re.M)
    else:
        s = s.rstrip("\n") + "\n" + linha + "\n"
put("BLING_USER", os.environ["BUSER"].replace('"', ''))
put("BLING_PASS", os.environ["BPASS"].replace('"', '\\"'))
open(".env", "w", encoding="utf-8").write(s)
PY
chmod 600 .env
rm -f bling_state.json          # sessão antiga do navegador do robô (se tinha): entra de novo com o login certo
echo "  login e senha do Bling gravados no .env da MedicalD"
unset BPASS BPASS2

echo "Situação 'Atendido' dos pedidos de compra no Bling da MedicalD (só lê):"
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
achou = []
for m in mods:
    if "compra" in nz(f"{m.get('nome')} {m.get('descricao')}"):
        r2 = b._get(f"/situacoes/modulos/{m['id']}")
        sits = ((r2.json() or {}).get("data") or []) if r2.status_code == 200 else []
        print(f"  {m.get('nome')} ({m['id']}): " + ", ".join(f"{s.get('id')}={s.get('nome')}" for s in sits), file=sys.stderr)
        achou += [s for s in sits if nz(s.get("nome")) in ("atendido", "atendida")]
if len(achou) == 1:
    print(f"SID={achou[0]['id']}")
PY
) || OUT=""
SID=$(printf '%s\n' "$OUT" | sed -n 's/^SID=\([0-9][0-9]*\)$/\1/p' | tail -1)
if [ -n "$SID" ]; then
  venv/bin/python - "$SID" <<'PY'
import re, sys
s = open(".env", encoding="utf-8").read()
k, v = "BLING_SITUACAO_PC_ATENDIDO_ID", sys.argv[1]
s = re.sub(rf"^\s*{k}\s*=.*$", f"{k}={v}", s, flags=re.M) if re.search(rf"^\s*{k}\s*=", s, re.M) else s.rstrip("\n") + f"\n# pedido de compra -> Atendido na entrada\n{k}={v}\n"
open(".env", "w", encoding="utf-8").write(s)
PY
  echo "  Atendido = $SID (gravado)"
else
  echo "  !! não achei a situação Atendido sem dúvida: a entrada vai avisar na prévia (manda a lista acima para o Claude)"
fi

echo "Navegador do robô na MedicalD:"
if venv/bin/python -c "from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); b.close()
print('  ok: Playwright e Chromium funcionam')" 2>/dev/null; then :; else
  echo "  faltou o Playwright/Chromium nesta pasta: instalando (só na MedicalD)"
  venv/bin/pip install -q playwright >/dev/null 2>&1 || true
  venv/bin/python -m playwright install chromium >/dev/null 2>&1 || true
  venv/bin/python -c "from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); b.close()
print('  ok agora')" || echo "  !! ainda não abre o navegador: o robô vai falhar; manda esta saída para o Claude"
fi

[ "$T_ROBO" = 1 ] && mv .robo_novo services/bling_entrada_robo.py || rm -f .robo_novo
[ "$T_RECEB" = 1 ] && mv .receb_novo recebimento_routes.py || rm -f .receb_novo
sudo systemctl restart "$SVC"
sleep 6
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/api/recebimento/resumo || true)
if ! systemctl is-active --quiet "$SVC" || ! { [ "$code" = 401 ] || [ "$code" = 200 ]; }; then
  bash "$BKD/desfazer.sh"; echo "A MedicalD não subiu (recebimento/resumo = $code): voltei tudo."; exit 1
fi
echo
echo "OK: MedicalD no ar (recebimento $code). LHVMed não foi tocada."
echo "Para testar: no WMS da MedicalD, Recebimento > nota nova > conferir > Lançar entrada. O robô entra no Bling da MedicalD"
echo "com o login gravado (se o Bling pedir código por e-mail/WhatsApp na 1ª vez, me manda o erro)."
echo "Desfazer:  bash $M/$BKD/desfazer.sh"
