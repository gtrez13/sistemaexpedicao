#!/usr/bin/env bash
# MedicalD com TUDO o que a LHVMed tem no backend do WMS (Biel 19/11: "pegar TUDO que fizemos para a LHVMed e jogar
# pra rodar TAMBÉM na MedicalD"). Uso:  bash ligar_medicald.sh <commit>
#
# O que faz (só na MedicalD: pasta EuropaD, wms-europa.service, porta 8001; a LHVMed é só LIDA):
#   1. backup: código da MedicalD (tgz), banco wms_medicald (pg_dump), .env, unidade do systemd, crontab, pacotes
#   2. monta uma CÓPIA de teste fora do ar com os módulos da LHVMed e confere: o app sobe, as rotas das telas novas
#      existem, o bling_service/ml_service da MedicalD têm o que os módulos chamam, leituras como admin dão 200.
#      Se algo grave faltar, PARA aqui e nada muda na MedicalD.
#   3. aplica de verdade: módulos que faltam (cópia exata da LHVMed), mapa_routes.py novo, robô da entrada novo,
#      blueprints no app.py, sessão igual à da LHVMed, correções do bling_service/ml_service/routes.py (com âncora),
#      valores da MedicalD no .env (depósito Local, CNPJ, Não achado zera o Bling, nota de devolução automática,
#      naturezas de devolução da MedicalD achadas no Bling dela, FEFO desligado)
#   4. 4 threads no gunicorn (igual à LHVMed), cron do estoque (a cada 5 min) e da virada do dia
#   5. reinicia a MedicalD; se não subir, volta tudo sozinho
# Desfazer: bash ~/backups_medicald/ligar_<data>/desfazer.sh
set -eo pipefail
REF="${1:?passe o commit: bash ligar_medicald.sh <commit>}"
L="$HOME/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix"
M="$HOME/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix"
SVC=wms-europa.service
PORTA=8001
G="https://raw.githubusercontent.com/gtrez13/sistemaexpedicao/$REF"
TS=$(date +%Y%m%d%H%M%S)
BKD="$HOME/backups_medicald/ligar_$TS"
PY="$M/venv/bin/python"
ROBO_HASH=7a081477bd35          # services/bling_entrada_robo.py: login digitado (b7149aa)
etapa() { echo; echo "== $*"; }
blob() { git hash-object "$1" 2>/dev/null | cut -c1-12 || python3 -c "import hashlib,sys;d=open(sys.argv[1],'rb').read();print(hashlib.sha1(b'blob %d\0'%len(d)+d).hexdigest()[:12])" "$1"; }

[ -f "$L/app.py" ] && [ -f "$M/app.py" ] && [ -x "$PY" ] || { echo "não achei as pastas do WMS da LHVMed e da MedicalD"; exit 1; }
UNIT=$(systemctl show -p FragmentPath "$SVC" | cut -d= -f2)
if [ -z "$UNIT" ] || ! grep -q "127.0.0.1:$PORTA" "$UNIT" || ! grep -q "WorkingDirectory=$M" "$UNIT"; then
  echo "a unidade $SVC não é a da MedicalD ($M, porta $PORTA): parei, nada mudou"; exit 1
fi
echo "LHVMed (só leitura): $L"
echo "MedicalD (vai mudar): $M  [$SVC, porta $PORTA]"
sudo -v

# ───────────────────────── 1. backup ─────────────────────────
etapa "1/6 Backup (nada mudou ainda)"
mkdir -p "$BKD/orig" "$BKD/novos"
chmod 700 "$HOME/backups_medicald" "$BKD"
tar czf "$BKD/codigo_medicald.tgz" -C "$M" --exclude=./venv --exclude='*.log' --exclude='__pycache__' \
    --exclude=./.bling_robot_profile --exclude=./downloads .
DBURL=$(grep -E '^[[:space:]]*DATABASE_URL[[:space:]]*=' "$M/.env" | tail -1 | cut -d= -f2- \
        | sed -E "s/^[[:space:]\"']+//; s/[[:space:]\"']+$//; s#^postgres(ql)?(\+[a-z0-9]+)?://#postgresql://#")
[ -n "$DBURL" ] || { echo "não achei o DATABASE_URL no .env da MedicalD: parei, nada mudou"; exit 1; }
pg_dump "$DBURL" -Fc -f "$BKD/wms_medicald.dump" || { echo "o backup do banco falhou: parei, nada mudou"; exit 1; }
unset DBURL
crontab -l > "$BKD/crontab.txt" 2>/dev/null || : > "$BKD/crontab.txt"
sudo cp -p "$UNIT" "$BKD/unit.service"
"$M/venv/bin/pip" freeze 2>/dev/null > "$BKD/pip_antes.txt" || true
: > "$BKD/registro.txt"; : > "$BKD/pip_adicionados.txt"
cat > "$BKD/desfazer.sh" <<EOF
#!/usr/bin/env bash
# Desfaz o ligar_medicald.sh de $TS: arquivos, .env, unidade do systemd, crontab e pacotes da MedicalD; reinicia a MedicalD.
cd "$M" || exit 1
while read -r t f; do
  case "\$t" in
    ADD) rm -f "\$f" ;;
    MOD) cp -p "$BKD/orig/\$f" "\$f" ;;
  esac
done < "$BKD/registro.txt"
sudo cp -p "$BKD/unit.service" "$UNIT" && sudo systemctl daemon-reload
crontab "$BKD/crontab.txt"
[ -s "$BKD/pip_adicionados.txt" ] && venv/bin/pip uninstall -y -r "$BKD/pip_adicionados.txt" >/dev/null 2>&1 || true
sudo systemctl restart $SVC; sleep 5
echo "MedicalD de volta como antes: \$(systemctl is-active $SVC), /api/auth/me \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORTA/api/auth/me)"
echo "(as tabelas novas do banco ficaram, vazias; o banco de antes está em $BKD/wms_medicald.dump)"
EOF
chmod +x "$BKD/desfazer.sh"
echo "backup em $BKD ($(du -sh "$BKD" | cut -f1))"

curl -fsSL "$G/medicald_aplicar.py" -o "$BKD/novos/medicald_aplicar.py"
curl -fsSL "$G/services/bling_entrada_robo.py" -o "$BKD/novos/bling_entrada_robo.py"
[ "$(blob "$BKD/novos/bling_entrada_robo.py")" = "$ROBO_HASH" ] || { echo "o robô baixado não é a versão esperada: parei, nada mudou"; exit 1; }
"$PY" -m py_compile "$BKD/novos/medicald_aplicar.py" "$BKD/novos/bling_entrada_robo.py"
AP="$BKD/novos/medicald_aplicar.py"

# ───────────────────────── 2. cópia de teste ─────────────────────────
etapa "2/6 Cópia de teste fora do ar (a MedicalD segue rodando como está)"
STG=$(mktemp -d /tmp/medicald_teste.XXXXXX)
trap 'rm -rf "$STG"' EXIT
tar -C "$M" --exclude=./venv --exclude='*.log' --exclude='__pycache__' --exclude=./.bling_robot_profile --exclude=./downloads -cf - . \
  | tar -C "$STG" -xf -
ln -s "$M/venv" "$STG/venv"

# pacotes Python que a LHVMed usa e a MedicalD não tem (só acrescenta; não troca versão de nenhum que já existe)
"$L/venv/bin/pip" freeze 2>/dev/null > "$BKD/pip_lhvmed.txt" || true
FALTAM=$("$PY" -I "$AP" pip --lhv "$BKD/pip_lhvmed.txt" --med "$BKD/pip_antes.txt" || true)
if [ -n "$FALTAM" ]; then
  echo "pacotes que faltam na MedicalD: $(echo $FALTAM)"
  for p in $FALTAM; do
    if "$M/venv/bin/pip" install -q --no-deps "$p" >/dev/null 2>&1; then echo "$p" >> "$BKD/pip_adicionados.txt"; else echo "  !! não instalou $p"; fi
  done
fi

# "Pronto para faturar" existe no Bling da MedicalD? (só lê as situações de venda)
FATURAR=""
"$PY" -I "$AP" aplicar --alvo "$STG" --lhv "$L" --robo "$BKD/novos/bling_entrada_robo.py" > "$BKD/teste_aplicar.txt" 2>&1 \
  || { cat "$BKD/teste_aplicar.txt"; echo "parei na cópia de teste: nada mudou na MedicalD"; [ -s "$BKD/pip_adicionados.txt" ] && "$M/venv/bin/pip" uninstall -y -r "$BKD/pip_adicionados.txt" >/dev/null 2>&1; exit 1; }
if (cd "$STG" && timeout 60 venv/bin/python -c "
from dotenv import load_dotenv; load_dotenv('.env')
import comercial_faturar as c; print('id', c.situacao_faturar_id())" 2>/dev/null | grep -q "^id [0-9]"); then
  FATURAR="--faturar"
  echo "Bling da MedicalD tem a situação 'Pronto para faturar': Comercial conferido vai para ela (igual à LHVMed)"
  "$PY" -I "$AP" aplicar --alvo "$STG" --lhv "$L" --robo "$BKD/novos/bling_entrada_robo.py" $FATURAR > /dev/null 2>&1 || true
else
  echo "não achei a situação 'Pronto para faturar' no Bling da MedicalD: o Comercial conferido não muda a situação no Bling"
fi
cat "$BKD/teste_aplicar.txt"
echo "-- naturezas da nota de devolução (só lê o Bling da MedicalD):"
(cd "$STG" && timeout 90 "$PY" -I "$AP" naturezas --alvo "$STG" > "$BKD/naturezas.env" 2> "$BKD/naturezas.txt") || echo "  (não consegui ler as naturezas)"
grep -v "^\s*[0-9]" "$BKD/naturezas.txt" | sed 's/^/  /' | tail -4 || true
echo "-- conferindo a cópia de teste:"
if ! (cd "$STG" && timeout 300 "$PY" -I "$AP" testar --alvo "$STG" --lhv "$L"); then
  if [ "${FORCAR:-0}" != "1" ]; then
    echo
    echo "PAREI: a cópia de teste mostrou problema (XX acima). Nada mudou na MedicalD. Manda esta saída para o Claude."
    [ -s "$BKD/pip_adicionados.txt" ] && "$M/venv/bin/pip" uninstall -y -r "$BKD/pip_adicionados.txt" >/dev/null 2>&1 && echo "(tirei os pacotes que tinha posto)"
    exit 1
  fi
  echo "FORCAR=1: sigo mesmo assim"
fi

# ───────────────────────── 3. aplica de verdade ─────────────────────────
etapa "3/6 Aplicando na MedicalD"
voltar() { echo; echo "deu erro no meio: voltando tudo"; bash "$BKD/desfazer.sh"; exit 1; }
trap 'rm -rf "$STG"; voltar' ERR
"$PY" -I "$AP" aplicar --alvo "$M" --lhv "$L" --robo "$BKD/novos/bling_entrada_robo.py" $FATURAR --extra-env "$BKD/naturezas.env" \
     --orig "$BKD/orig" --registro "$BKD/registro.txt" > "$BKD/aplicar.txt" 2>&1 || { cat "$BKD/aplicar.txt"; voltar; }
grep -v "diferentes nas duas" "$BKD/aplicar.txt" | grep -E "^ +(ok|!!)" | head -20 || true

# ───────────────────────── 4. threads + cron ─────────────────────────
etapa "4/6 Gunicorn com 4 threads e cron (estoque a cada 5 min, virada do dia)"
if grep -qE "ExecStart=.*--threads 1( |$)" "$UNIT"; then
  sudo sed -i -E '/^ExecStart=/ s/--threads 1( |$)/--threads 4\1/' "$UNIT"
  sudo systemctl daemon-reload
  echo "wms-europa: --threads 4 (antes 1: um pedido fechando travava todo o resto)"
else
  echo "threads: deixei como está ($(grep -oE -- '--threads [0-9]+' "$UNIT" || echo 'sem --threads'))"
fi
echo "estoque (teste sem gravar):"
(cd "$M" && timeout 180 venv/bin/python estoque_sync.py --dry 2>&1 | grep -vi warn | tail -4) || echo "  (o teste do estoque falhou: o cron fica, mas manda isto para o Claude)"
echo "virada do dia (prévia, só lê):"
(cd "$M" && timeout 180 venv/bin/python -u virada_dia.py --previa 2>&1 | tail -6) || true
LOGE="$HOME/nexuswms/estoque_sync_medicald.log"
( grep -v "# medicald_wms" "$BKD/crontab.txt" || true
  echo "2-59/5 * * * * cd $M && flock -n /tmp/estoque_sync_medicald.lock venv/bin/python estoque_sync.py 2>&1 | grep -v -i warn >> $LOGE # medicald_wms"
  echo "12,42 * * * * cd $M && venv/bin/python -u virada_dia.py --cron >> virada_dia.log 2>&1 # medicald_wms"
  echo "5 4 * * 0 tail -n 2000 $LOGE > $LOGE.tmp && mv $LOGE.tmp $LOGE # medicald_wms"
) | crontab -
echo "cron: $(crontab -l | grep -c '# medicald_wms') linhas da MedicalD (as da LHVMed ficaram iguais: $(crontab -l | grep -vc '# medicald_wms'))"

# ───────────────────────── 5. reinicia e confere ─────────────────────────
etapa "5/6 Reiniciando a MedicalD"
trap 'rm -rf "$STG"' ERR
sudo systemctl restart "$SVC"
sleep 8
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORTA/api/auth/me" || true)
if ! systemctl is-active --quiet "$SVC" || ! { [ "$CODE" = 200 ] || [ "$CODE" = 401 ]; }; then
  echo "a MedicalD não subiu com os módulos (auth/me = $CODE):"
  sudo journalctl -u "$SVC" --since "-60 s" --no-pager | tail -25
  voltar
fi
echo "MedicalD no ar: /api/auth/me $CODE"
for r in recebimento/resumo full/remessas tarefas saida/lista nao-achado/resumo devolucoes/resumo mapa/layout inicio/resumo estoque/status; do
  printf '  /api/%-20s %s\n' "$r" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORTA/api/$r")"
done
echo "  (401 = a rota existe e pede login; 404 = não carregou)"
sudo journalctl -u "$SVC" --since "-60 s" --no-pager | grep -iE "não carregou|Traceback|Error" | tail -8 || true
echo "LHVMed (não mexi): $(systemctl is-active wms-russia.service), /api/auth/me $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/auth/me)"

# ───────────────────────── 6. o que falta configurar ─────────────────────────
etapa "6/6 Naturezas da nota de devolução no Bling da MedicalD"
cat "$BKD/naturezas.txt" 2>/dev/null || echo "  (não li)"
echo
echo "PRONTO. Backup: $BKD"
echo "Desfazer tudo (1 comando):  bash $BKD/desfazer.sh"
