#!/usr/bin/env bash
# Mapa montável (Biel 16/11): o admin monta o mapa do galpão na tela Mapa (porta-pallets, cestas, refrigeradores, corredores,
# posições de cada um e a ordem da rota). Sem mapa salvo vale o mapa da LHVMed de sempre: mesmos códigos, placas LOC- e ordem.
# Atualiza mapa_routes.py + mapa_layout.py (novo) + full_routes.py + full_auto.py, e recebimento_routes.py se for o de sempre.
# Backup de tudo numa pasta + desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
[ -f full_routes.py ] || { echo "Rode antes o ligar_full.sh (o mapa novo já traz o Full junto)."; exit 1; }
REF="${1:?passe o commit}"
BASE_REC=44cf75c                      # recebimento_routes.py de antes desta mudança
TS=$(date +%Y%m%d%H%M%S)
BKD="bak_mapa_$TS"
URL="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
mkdir "$BKD"
for f in mapa_routes.py full_routes.py full_auto.py recebimento_routes.py; do [ -f "$f" ] && cp -p "$f" "$BKD/"; done
for f in mapa_routes.py mapa_layout.py full_routes.py full_auto.py recebimento_routes.py; do curl -fsSL "$URL/$f" -o "$f.novo"; done
for f in mapa_routes.py mapa_layout.py full_routes.py full_auto.py recebimento_routes.py; do venv/bin/python -m py_compile "$f.novo"; done

# recebimento: só troca se o do servidor for o de sempre (não atropela ajuste feito direto aqui)
if curl -fsSL "https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$BASE_REC/recebimento_routes.py" -o .rec_base 2>/dev/null \
   && cmp -s .rec_base recebimento_routes.py; then
  mv recebimento_routes.py.novo recebimento_routes.py; REC="atualizado"
else
  rm -f recebimento_routes.py.novo; REC="NÃO trocado (o do servidor é diferente do esperado; o mapa funciona igual)"
fi
rm -f .rec_base
for f in mapa_routes.py mapa_layout.py full_routes.py full_auto.py; do mv "$f.novo" "$f"; done

sudo systemctl restart wms-russia.service
sleep 4
if ! systemctl is-active --quiet wms-russia.service; then
  cp -p "$BKD"/* .
  rm -f mapa_layout.py
  sudo systemctl restart wms-russia.service
  echo "O serviço não subiu com os arquivos novos: voltei tudo como estava. Nada mudou."
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/mapa/layout)
echo "OK: serviço no ar, /api/mapa/layout respondeu $code (401 = existe, pede login)"
echo "recebimento_routes.py: $REC"
venv/bin/python - <<'PY' || true
import mapa_layout as M
L = M.layout_padrao()
n = sum(len(M.posicoes(e)) for e in L["estruturas"])
print(f"Mapa padrão (o da LHVMed de hoje): {len(L['estruturas'])} estruturas, {n} posições.",
      "Ex.:", M.rotulo(104, 4), "|", M.rotulo(7, 4), "|", M.rotulo(51, 1))
PY
echo "Para montar: tela Mapa > Montar mapa (só aparece para admin). Toda vez que salva vira uma versão nova; dá para voltar qualquer uma."
echo
echo "Desfazer:"
echo "  cp -p $BKD/* . && rm -f mapa_layout.py && sudo systemctl restart wms-russia.service"
