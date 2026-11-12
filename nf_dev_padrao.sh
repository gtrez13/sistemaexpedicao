#!/usr/bin/env bash
# E-mail e telefone que a nota de devolução usa quando o cliente não tem (cliente do ML nunca tem).
# Sem argumentos: pega os da PRÓPRIA LHVMED no Bling (e-mail da empresa + telefone do emitente das notas).
# Ou passe à mão:  bash nf_dev_padrao.sh EMAIL TELEFONE
# Backup do .env + 1 comando para desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac

EM="$1"; TEL="$2"
if [ -z "$EM" ]; then
  OUT=$(venv/bin/python -W ignore - <<'PY'
import re, sys, warnings
warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import requests
from services.bling_service import BlingService
b = BlingService()
email, tel = "", ""
try:
    d = b._get_json("/empresas/me/dados-basicos").get("data") or {}
    email = str(d.get("email") or "").strip()
except Exception as e:
    print(f"(dados da empresa no Bling: {str(e)[:120]})", file=sys.stderr)
try:
    for n in (b._get_json("/nfe", params={"tipo": 1, "limite": 15}).get("data") or []):
        det = b._get_json(f"/nfe/{n['id']}").get("data") or {}
        url = det.get("xml")
        if not url:
            continue
        xml = requests.get(url, timeout=20).text
        emit = re.search(r"<emit>(.*?)</emit>", xml, re.S)      # só o bloco do EMITENTE (nunca o do cliente)
        emit = emit.group(1) if emit else ""
        m = re.search(r"<fone>(\d{10,11})</fone>", emit)
        if not email:
            me = re.search(r"<email>([^<]+@[^<]+)</email>", emit)
            email = me.group(1).strip() if me else email
        if m:
            tel = m.group(1)
            break
except Exception as e:
    print(f"(XML das notas: {str(e)[:120]})", file=sys.stderr)
print(f"{email} {tel}")
PY
)
  EM=$(echo "$OUT" | awk '{print $1}'); TEL=$(echo "$OUT" | awk '{print $2}')
  echo "Achei no Bling (dados da LHVMED): e-mail '${EM:-nenhum}' · telefone '${TEL:-nenhum}'"
fi
TEL="$(printf '%s' "$TEL" | tr -dc '0-9')"
printf '%s' "$EM" | grep -qE '^[^@ ]+@[^@ ]+\.[^@ ]+$' || { echo "Não achei um e-mail válido. Rode passando à mão: bash nf_dev_padrao.sh email@lhvmed.com.br 11999998888"; exit 1; }
[ ${#TEL} -ge 10 ] && [ ${#TEL} -le 11 ] || { echo "Não achei um telefone válido. Rode passando à mão: bash nf_dev_padrao.sh $EM 11999998888"; exit 1; }

BK=".env.bak_nfdev_$(date +%Y%m%d%H%M%S)"
cp .env "$BK"
sed -i '/^NF_DEV_EMAIL_PADRAO=/d;/^NF_DEV_TELEFONE_PADRAO=/d' .env
printf '\nNF_DEV_EMAIL_PADRAO=%s\nNF_DEV_TELEFONE_PADRAO=%s\n' "$EM" "$TEL" >> .env
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — padrão da nota de devolução: $EM / $TEL ($(systemctl is-active wms-russia.service))"
echo "Para desfazer: cp $BK .env && sudo systemctl restart wms-russia.service"
