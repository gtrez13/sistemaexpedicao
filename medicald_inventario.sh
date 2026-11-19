#!/usr/bin/env bash
# SÓ LEITURA. Compara o backend da MedicalD com o da LHVMed para levar tudo da LHVMed para a MedicalD (Biel 19/11).
# Não muda nada: lista arquivos (iguais / diferentes / faltando), blueprints do app.py, nomes das variáveis do .env (sem valor),
# drop-ins do systemd (sem valor), crontab, tabelas do banco e, no Bling da MedicalD, CNPJ/razão social, depósitos e formas
# de pagamento. Gera um arquivo para mandar para o Claude.
L=~/nexuswms/RussianL-country/sistemaexpedicao_fix_patched/sistemaexpedicao_fix
M=~/nexuswms/EuropaD-country/europad-wmm/sistemaexpedicao_fix
OUT=~/inventario_medicald_$(date +%Y%m%d%H%M%S).txt
[ -d "$L" ] && [ -d "$M" ] || { echo "não achei as pastas: $L / $M"; exit 1; }
h() { python3 -c "import hashlib,sys; d=open(sys.argv[1],'rb').read(); print(hashlib.sha1(b'blob %d\0' % len(d) + d).hexdigest()[:12])" "$1"; }
{
echo "== pastas"; echo "LHVMed:   $L"; echo "MedicalD: $M"; date
echo; echo "== arquivos .py e .sh (L = LHVMed, M = MedicalD)"
( cd "$L"; ls *.py services/*.py 2>/dev/null; cd "$M"; ls *.py services/*.py 2>/dev/null ) | sort -u | while read f; do
  a=""; b=""
  [ -f "$L/$f" ] && a=$(h "$L/$f"); [ -f "$M/$f" ] && b=$(h "$M/$f")
  if [ -z "$b" ]; then st="FALTA NA MEDICALD"; elif [ -z "$a" ]; then st="só na MedicalD"; elif [ "$a" = "$b" ]; then st="igual"; else st="DIFERENTE"; fi
  printf '%-45s %-20s L=%s M=%s\n' "$f" "$st" "${a:--}" "${b:--}"
done
echo; echo "== app.py da MedicalD: blueprints e imports de rotas"
grep -n "register_blueprint\|import .*_routes\|from .*_routes\|registrar(\|session_protection\|PERMANENT_SESSION" "$M/app.py"
echo; echo "== app.py da LHVMed: blueprints e imports de rotas"
grep -n "register_blueprint\|import .*_routes\|from .*_routes\|registrar(\|session_protection\|PERMANENT_SESSION" "$L/app.py"
echo; echo "== fim do mapa_routes.py (o que liga módulos)"
echo "-- LHVMed"; tail -25 "$L/mapa_routes.py" 2>/dev/null | grep -n "import\|registrar\|register" ; echo "-- MedicalD"; tail -25 "$M/mapa_routes.py" 2>/dev/null | grep -n "import\|registrar\|register"
echo; echo "== variáveis do .env (só os nomes)"
for x in L M; do d=$L; [ $x = M ] && d=$M; echo "-- $x"; sed -n 's/^\s*\([A-Za-z_][A-Za-z0-9_]*\)\s*=.*/\1/p' "$d/.env" 2>/dev/null | sort | tr '\n' ' '; echo; done
echo; echo "== systemd (só nomes das variáveis)"
for s in wms-russia wms-europa; do echo "-- $s"; sudo grep -h "ExecStart\|EnvironmentFile\|WorkingDirectory" /etc/systemd/system/$s.service 2>/dev/null
  sudo grep -rh "Environment" /etc/systemd/system/$s.service.d/ 2>/dev/null | sed 's/=\([A-Za-z_]*\)=.*/=\1=***/'; done
echo; echo "== crontab"
crontab -l 2>/dev/null | grep -v "^#" | sed 's/\(TOKEN\|PASS\|SECRET\)=[^ ]*/\1=***/g'
echo; echo "== banco: tabelas (linhas aproximadas)"
for x in L M; do d=$L; [ $x = M ] && d=$M; echo "-- $x"; ( cd "$d"; venv/bin/python - <<'PY' 2>&1 | tail -60
import os
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception: pass
import psycopg2
u = os.environ.get("DATABASE_URL")
if not u: print("sem DATABASE_URL"); raise SystemExit
c = psycopg2.connect(u).cursor()
c.execute("SELECT relname, reltuples::bigint FROM pg_class WHERE relkind='r' AND relnamespace='public'::regnamespace ORDER BY relname")
print(" ".join(f"{n}({max(t,0)})" for n, t in c.fetchall()))
PY
) ; done
echo; echo "== Bling da MedicalD (só leitura)"
( cd "$M"; venv/bin/python - <<'PY' 2>&1 | tail -40
import json, os
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception: pass
from services.bling_service import BlingService
b = BlingService()
def g(p, **k):
    try: return b._get_json(p, params=k or None)
    except Exception as e: return {"erro": str(e)[:200]}
e = g("/empresas/me/dados-basicos").get("data") or {}
print("empresa:", e.get("razaoSocial") or e.get("nome"), "| CNPJ:", e.get("cnpj"), "| cidade:", (e.get("endereco") or {}).get("municipio"))
print("depósitos:", [(d.get("id"), d.get("descricao"), d.get("padrao")) for d in (g("/depositos").get("data") or [])])
print("formas de pagamento:", [(f.get("id"), f.get("descricao"), f.get("tipoPagamento")) for f in (g("/formas-pagamentos", limite=100).get("data") or [])][:20])
print("lojas/canais:", [(x.get("id"), x.get("descricao"), x.get("tipo")) for x in (g("/canais-venda").get("data") or [])][:20])
PY
)
echo; echo "== serviço e portas"
systemctl is-active wms-russia.service wms-europa.service; sudo ss -ltnp 2>/dev/null | grep -E ":800[01] " | sed 's/users:.*//'
} > "$OUT" 2>&1
chmod 600 "$OUT"
echo "Pronto (não mudou nada). Manda este arquivo para o Claude: $OUT"
wc -l "$OUT"
