#!/usr/bin/env bash
# Etiqueta única por unidade (Biel 16/11 + 18/11), LHVMed:
#  - Recebimento: cada etiqueta impressa tem código próprio (QR SKU*LOTE*U1234ABC); a conferência é só colando e bipando cada
#    etiqueta, em toda nota (sem "Está tudo certo"). RECEB_ETQ_UNICA=fim = confere como antes e bipa no fim; 0 = desliga.
#  - Depois do recebimento (18/11): Separação, Expedição e Full não contam a mesma etiqueta duas vezes no mesmo dia;
#    Rastrear mostra a trilha da unidade e os bips repetidos por pessoa.
# Troca só o recebimento_routes.py. Antes de trocar, confere se o do servidor é uma versão conhecida (do git):
# se for outra coisa (alguém mexeu no servidor), NÃO troca e mostra a diferença. FORCAR=1 troca mesmo assim (com backup).
# Backup + desfazer; se o serviço não subir, volta sozinho.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
REF="${1:?passe o commit}"
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao"
PY=venv/bin/python; [ -x "$PY" ] || PY=python3

# versões do recebimento_routes.py que já saíram do git (hash do conteúdo -> commit)
CONHECIDAS="
7f26b35e676410b6af5f00461dbdd3d43091e5af 7ca9eee
83e138e522a2ae958eaa3323318a7046b8921b67 d989880
ada4c84287753bbfbf2741fc014d8cb15c29e05e 88691e2
cdedcbb14aaee5fe606e254921b55b4ff4df0a82 56c07a7
c5ae48768459ffeeebfce322f0593c3767ba11bf cf0b67a
9e6f59d0f41d104e2631731c541984c7628cdd28 3bce6b0
364f37e0d2c4ebacf02a22026271eae0012eccc8 17ca7f3
6364f276856f88feabc59c3ebc31d48a3e035a55 5e6907a
8318ee175d6887689413e1d43f93a2d3936e5683 f517e47
10cb123c8186a735e559efed8940a4e9f864af35 25a68ad
10eaf1e5670e83e57932b0ab89f5e49c749756ea ff58cd8
2dc5e6457413ca6e1390e60a67a339cfe21b2e69 8c4432b
56dcf02409ab1c8e451d4e78976993857384ea29 36ec734
35367e8ca51e480ec165d182c119013d90d525de 1cd1bfc
9ae65ca73dc56fe314a430418e5619fc6b04037c 9f1e796
5745d151581dbf5a46b04c3f3c33b4367721af84 dd77602
5dd3e334234ca06d69bdce69759bf3c2aabd2643 47eb27b
043d8cffdb15ba8b4b72c33b35a729fc85cf20e7 69ded3a
077975af1c31c083320b0cd705c142ea5013f9a5 dd992c9
f8654134ab54e2f3f223bfc982c23473d0de5064 1efcd85
43551a77caa62caeee1fcfdf2dd4cd49ac50dce4 f251b52
9ff5d487d7f5ad1c3654aea55f1ff6c6506dd8e7 dcaa82b
b51739710024a70ee1c4dcf80aa5e7ff23da3009 85ee75d
3ed68e4c8b1e066935b5fc9b991b7237790b5063 1ee6695"
hash_git() { "$PY" -c "import hashlib,sys; d=open(sys.argv[1],'rb').read(); print(hashlib.sha1(b'blob %d\0' % len(d) + d).hexdigest())" "$1"; }

H=$(hash_git recebimento_routes.py)
ATUAL=$(echo "$CONHECIDAS" | awk -v h="$H" '$1==h {print $2}')
curl -fsSL "$G/$REF/recebimento_routes.py" -o recebimento_routes.py.novo
"$PY" -m py_compile recebimento_routes.py.novo
NOVO=$(hash_git recebimento_routes.py.novo)
if [ "$H" = "$NOVO" ]; then
  rm -f recebimento_routes.py.novo
  echo "O recebimento_routes.py do servidor já é o $REF. Nada a fazer."; exit 0
fi
if [ -n "$ATUAL" ]; then
  echo "recebimento_routes.py do servidor = versão $ATUAL do git (conhecida). Pode trocar."
elif [ "${FORCAR:-0}" = "1" ]; then
  echo "recebimento_routes.py do servidor NÃO é uma versão do git, mas FORCAR=1: troco mesmo assim (com backup)."
else
  echo "PAREI: o recebimento_routes.py do servidor não é nenhuma versão do git (alguém mexeu nele no servidor)."
  echo "Não troquei nada. Comparando com as últimas versões para achar a mais parecida:"
  MELHOR=""; MENOR=999999
  for c in 56c07a7 cf0b67a 3bce6b0 17ca7f3 5e6907a; do
    curl -fsSL "$G/$c/recebimento_routes.py" -o ".rr_$c" || continue
    n=$(diff ".rr_$c" recebimento_routes.py | grep -c '^[<>]' || true)
    echo "  $c: $n linhas diferentes"
    [ "$n" -lt "$MENOR" ] && { MENOR=$n; MELHOR=$c; }
  done
  if [ -n "$MELHOR" ]; then
    diff ".rr_$MELHOR" recebimento_routes.py > recebimento_routes.servidor.diff || true
    echo "Diferença para a $MELHOR (o que só existe no servidor = linhas com >):"
    head -60 recebimento_routes.servidor.diff; echo "(fim; completo em recebimento_routes.servidor.diff — me mande)"
  fi
  rm -f .rr_* recebimento_routes.py.novo
  echo "Se for para trocar mesmo assim: FORCAR=1 bash ligar_etq_unica.sh $REF"
  exit 1
fi
# o arquivo novo usa o mapa montável e o robô de conciliação do Bling: confere antes
[ -f mapa_layout.py ] || echo "Aviso: sem mapa_layout.py (ligar_dia.sh). O endereço sai no formato antigo; o resto funciona."
grep -q "def conciliar_pedido" services/bling_entrada_robo.py 2>/dev/null || echo "Aviso: services/bling_entrada_robo.py sem conciliar_pedido: a conciliação pelo robô do Bling vai falhar e o lançamento segue sem conciliar."

BK="recebimento_routes.py.bak_etq_$(date +%Y%m%d%H%M%S)"
cp -p recebimento_routes.py "$BK"
mv recebimento_routes.py.novo recebimento_routes.py
sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp -p "$BK" recebimento_routes.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com o arquivo novo: voltei o recebimento_routes.py de antes. Nada mudou."
  sudo journalctl -u wms-russia.service -n 20 --no-pager | tail -20
  exit 1
fi
c1=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/api/recebimento/notas/0/etiquetas/codigos || true)
c2=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8000/api/recebimento/unidade/usar || true)
c3=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/recebimento/notas || true)
echo "OK: serviço no ar. Etiquetas $c1, unidade $c2, lista de notas $c3 (401 = existe, pede login)."
"$PY" - <<'PY' || true
import recebimento_routes as R
R._vigia_ligar = lambda: None
R.garantir_tabelas()
with R.db_session() as c:
    n = c.execute("SELECT COUNT(*) AS n FROM receb_etq").fetchone()["n"]
    u = c.execute("SELECT COUNT(*) AS n FROM etq_uso").fetchone()["n"]
print(f"Tabelas prontas: {n} etiqueta(s) com código já impressa(s), {u} unidade(s) contada(s) depois do recebimento.")
print(f"Modo: RECEB_ETQ_UNICA={R.ETQ_UNICA} (estrito = só colando e bipando a etiqueta; fim = confere como antes e bipa no fim; 0 = desliga)")
PY
echo
echo "Desfazer:"
echo "  cp -p $BK recebimento_routes.py && sudo systemctl restart wms-russia.service"
