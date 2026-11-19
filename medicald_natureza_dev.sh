#!/usr/bin/env bash
# Nota de devolução da MedicalD com a natureza que a MedicalD já usa hoje (Biel 19/11: "usa do jeito que está hoje").
# Lê no Bling da MedicalD (só leitura) as notas de ENTRADA de devolução (finalidade 4) dos últimos 180 dias, vê a natureza
# de cada uma (dentro e fora de SP) e põe a mais usada no .env da MedicalD:
#   BLING_NATUREZA_DEV_NC_SP / BLING_NATUREZA_DEV_NC_FORA  (cliente CPF / sem IE, igual à LHVMed)
# Se só achar de um lado, usa a mesma nos dois. Sem nenhuma nota de devolução: não muda nada.
# Backup do .env + 1 comando para desfazer. Só mexe na MedicalD (wms-europa, porta 8001).
set -eo pipefail
M="$HOME/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix"
SVC=wms-europa.service
cd "$M" || { echo "não achei a pasta da MedicalD"; exit 1; }
[ -x venv/bin/python ] || { echo "sem venv na MedicalD"; exit 1; }
TMP=$(mktemp)
venv/bin/python -W ignore - "$TMP" <<'PY'
import os, sys, time, collections, datetime
from dotenv import load_dotenv
load_dotenv(".env")
sys.path.insert(0, os.getcwd())
from services.bling_service import BlingService
b = BlingService()
saida = sys.argv[1]
nat = {}
for pag in range(1, 4):
    d = (b._get_json("/naturezas-operacoes", params={"limite": 100, "pagina": pag}) or {}).get("data") or []
    for x in d:
        nat[str(x.get("id"))] = str(x.get("descricao") or "")
    if len(d) < 100:
        break
ini = (datetime.date.today() - datetime.timedelta(days=180)).isoformat()
notas = []
for pag in range(1, 11):
    d = (b._get_json("/nfe", params={"tipo": 0, "dataEmissaoInicial": ini + " 00:00:00", "limite": 100, "pagina": pag}) or {}).get("data") or []
    notas += d
    if len(d) < 100:
        break
    time.sleep(0.4)
print(f"notas de entrada nos últimos 180 dias: {len(notas)}")
cont = {"SP": collections.Counter(), "FORA": collections.Counter()}
vistos = 0
for n in sorted(notas, key=lambda x: str(x.get("dataEmissao") or ""), reverse=True)[:150]:
    time.sleep(0.35)
    try:
        det = (b._get_json(f"/nfe/{n['id']}") or {}).get("data") or {}
    except Exception:
        continue
    vistos += 1
    if int(det.get("finalidade") or 0) != 4:
        continue
    nid = str((det.get("naturezaOperacao") or {}).get("id") or "")
    if not nid:
        continue
    uf = str(((det.get("contato") or {}).get("endereco") or {}).get("uf") or "").upper()
    cont["SP" if uf == "SP" else "FORA"][nid] += 1
print(f"olhei {vistos} notas; devoluções: dentro de SP {sum(cont['SP'].values())}, fora de SP {sum(cont['FORA'].values())}")
for lado in ("SP", "FORA"):
    for nid, q in cont[lado].most_common(4):
        print(f"   {'dentro de SP' if lado == 'SP' else 'fora de SP  '}  {q:3}x  {nid}  {nat.get(nid, '(natureza não encontrada)')}")
sp = cont["SP"].most_common(1)[0][0] if cont["SP"] else None
fo = cont["FORA"].most_common(1)[0][0] if cont["FORA"] else None
sp, fo = sp or fo, fo or sp
with open(saida, "w") as f:
    if sp:
        f.write(f"{sp} {fo}\n")
        print(f"vai usar: dentro de SP = {nat.get(sp, sp)}; fora de SP = {nat.get(fo, fo)}")
    else:
        print("nenhuma nota de devolução de entrada encontrada: não mudo nada (a nota de cliente CPF/sem IE para na prévia)")
PY
read -r SP FO < "$TMP" || true
rm -f "$TMP"
[ -n "${SP:-}" ] || exit 0
BK=".env.bak_natdev_$(date +%Y%m%d%H%M%S)"
cp -p .env "$BK"
venv/bin/python - "$SP" "$FO" <<'PY'
import re, sys
sp, fo = sys.argv[1], sys.argv[2]
s = open(".env", encoding="utf-8").read()
for k, v in (("BLING_NATUREZA_DEV_NC_SP", sp), ("BLING_NATUREZA_DEV_NC_FORA", fo)):
    linha = f'{k}="{v}"'
    if re.search(rf"^\s*{k}\s*=", s, re.M):
        s = re.sub(rf"^\s*{k}\s*=.*$", linha, s, flags=re.M)
    else:
        s = s.rstrip("\n") + "\n" + linha + "\n"
open(".env", "w", encoding="utf-8").write(s)
PY
sudo systemctl restart "$SVC"
sleep 6
if ! systemctl is-active --quiet "$SVC"; then
  cp -p "$BK" .env; sudo systemctl restart "$SVC"
  echo "a MedicalD não subiu: voltei o .env"; exit 1
fi
echo "OK: MedicalD no ar ($(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/api/auth/me)). Nota de devolução usa essas naturezas."
echo "Desfazer: cd $M && cp -p $BK .env && sudo systemctl restart $SVC"
