#!/usr/bin/env bash
# Robô da entrada (Recebimento): conserta "'BlingRPA' object has no attribute 'garantir_logado'" (Biel 18/11, NF 13003).
# Troca só o services/bling_entrada_robo.py (login e espera da tela do Bling dentro dele, sem depender do bling_nfe_rpa.py).
# Confere antes se o do servidor é uma versão do git; se alguém mexeu nele, para e mostra a diferença (FORCAR=1 troca mesmo assim).
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
ARQ=services/bling_entrada_robo.py
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao"
PY=venv/bin/python; [ -x "$PY" ] || PY=python3
CONHECIDAS="
2f482c56b080a3721fd829dcf2a5196778f6235f d3cbbe6
e36154250bc422df717190d4719ad7ddb960763e 2eff668
7cb84bb2483afcf626ddb436455980d906c19f14 355b9a6
55e31eede581cdb4e31328fdc0cd2a1b09e58614 5e6907a
98265c33e79a09011bd51c3bcace890a39ab5264 8c4432b"
hash_git() { "$PY" -c "import hashlib,sys; d=open(sys.argv[1],'rb').read(); print(hashlib.sha1(b'blob %d\0' % len(d) + d).hexdigest())" "$1"; }
curl -fsSL "$G/$REF/$ARQ" -o "$ARQ.novo"
"$PY" -m py_compile "$ARQ.novo"
if [ -f "$ARQ" ]; then
  H=$(hash_git "$ARQ")
  if [ "$H" = "$(hash_git "$ARQ.novo")" ]; then rm -f "$ARQ.novo"; echo "Já está instalado ($REF). Nada mudou."; exit 0; fi
  ATUAL=$(echo "$CONHECIDAS" | awk -v h="$H" '$1==h {print $2}')
  if [ -n "$ATUAL" ]; then echo "$ARQ do servidor = versão $ATUAL do git (conhecida). Pode trocar."
  elif [ "${FORCAR:-0}" != "1" ]; then
    echo "PAREI: o $ARQ do servidor não é nenhuma versão do git. Não troquei nada."
    curl -fsSL "$G/5e6907a/$ARQ" -o .robo_base && { diff .robo_base "$ARQ" | head -60; echo "(fim da diferença para a 5e6907a — me mande)"; }
    rm -f .robo_base "$ARQ.novo"; echo "Para trocar mesmo assim: FORCAR=1 bash ligar_robo_entrada.sh $REF"; exit 1
  fi
fi
BK="$ARQ.bak_robo_$(date +%Y%m%d%H%M%S)"
[ -f "$ARQ" ] && cp -p "$ARQ" "$BK"
mv "$ARQ.novo" "$ARQ"
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  [ -f "$BK" ] && cp -p "$BK" "$ARQ"; sudo systemctl restart wms-russia.service
  echo "O serviço não subiu: voltei o $ARQ de antes. Nada mudou."; exit 1
fi
"$PY" -c "import sys; sys.path.insert(0,'.'); from services.bling_entrada_robo import _Tela; t=_Tela(); print('OK: robô carregou (login próprio:', callable(t.garantir_logado), ')')"
[ -n "$(grep -s '^BLING_USER=' .env)" ] && echo "BLING_USER está no .env (o robô entra sozinho se a sessão cair)." || echo "Aviso: BLING_USER não está no .env: se a sessão do Bling cair, o robô para pedindo login."
echo
echo "Agora: na NF 13003, toque em Continuar (o robô tenta conciliar com o pedido 3299 de novo)."
echo
echo "Desfazer:"
echo "  cp -p $BK $ARQ && sudo systemctl restart wms-russia.service"
