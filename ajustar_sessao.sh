#!/usr/bin/env bash
# Login caindo rápido (Biel 13/11). Duas causas no app.py:
#  1) session_protection = "strong": o Flask-Login DESLOGA (e apaga o "lembrar de mim") quando o IP muda
#     (celular trocando Wi-Fi/4G, proxy da Vercel/Cloudflare) -> passa para "basic" (só marca a sessão como "não recente").
#  2) a sessão não era permanente: o cookie morria ao fechar o navegador (Android fecha o Chrome sozinho) e os 12 h
#     do PERMANENT_SESSION_LIFETIME nunca valiam -> sessão permanente, renovada a cada uso.
# Resultado: quem está usando não cai; desloga depois de SESSAO_HORAS (padrão 12) SEM usar, ou no botão Sair.
# Backup do app.py + 1 comando para desfazer. Se o serviço não subir, volta sozinho.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac
grep -q "_sessao_permanente" app.py && { echo "o app.py já está ajustado: não mexi"; exit 0; }
BK="app.py.bak_$(date +%Y%m%d%H%M%S)"
cp app.py "$BK"
venv/bin/python - <<'PY'
import sys
s = open("app.py", encoding="utf-8", newline="").read()
fim = "\r\n" if "\r\n" in s else "\n"            # mantém o fim de linha do arquivo
trocas = [
    ('        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),\n',
     '        # sessão (e "lembrar de mim") valem SESSAO_HORAS SEM usar; cada uso renova (ajustar_sessao.sh, 13/11)\n'
     '        PERMANENT_SESSION_LIFETIME=timedelta(hours=float(os.getenv("SESSAO_HORAS", "12"))),\n'
     '        SESSION_REFRESH_EACH_REQUEST=True,\n'
     '        REMEMBER_COOKIE_DURATION=timedelta(hours=float(os.getenv("SESSAO_HORAS", "12"))),\n'
     '        REMEMBER_COOKIE_REFRESH_EACH_REQUEST=True,\n'
     '        REMEMBER_COOKIE_HTTPONLY=True,\n'
     '        REMEMBER_COOKIE_SAMESITE="Lax",\n'
     '        REMEMBER_COOKIE_SECURE=is_prod,\n'),
    ('    login_manager.session_protection = "strong"\n',
     '    # "strong" deslogava quando o IP mudava (celular Wi-Fi/4G, proxy da Vercel): "basic" só marca como não recente\n'
     '    login_manager.session_protection = "basic"\n'
     '\n'
     '    from flask import session as _sessao\n'
     '\n'
     '    @app.before_request\n'
     '    def _sessao_permanente():\n'
     '        _sessao.permanent = True   # cookie com validade (SESSAO_HORAS), renovada a cada uso; antes morria ao fechar o navegador\n'),
]
trocas = [(v.replace("\n", fim), n.replace("\n", fim)) for v, n in trocas]
for velho, novo in trocas:
    if s.count(velho) != 1:
        sys.exit(f"não achei no app.py (nada foi mudado): {velho.strip()}")
for velho, novo in trocas:
    s = s.replace(velho, novo)
open("app.py", "w", encoding="utf-8", newline="").write(s)
print("app.py ajustado")
PY
venv/bin/python -m py_compile app.py || { cp "$BK" app.py; echo "erro de sintaxe: voltei o app.py como estava"; exit 1; }
sudo systemctl restart wms-russia.service
sleep 5
if ! systemctl is-active --quiet wms-russia.service; then
  cp "$BK" app.py; sudo systemctl restart wms-russia.service
  echo "o serviço não subiu com o ajuste: voltei o app.py como estava"; exit 1
fi
echo "OK — /api/auth/me respondeu $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/auth/me) (200 ou 401 = no ar)"
echo "Quem já está logado continua logado. Para mudar as horas: SESSAO_HORAS=24 no .env e reiniciar o serviço."
echo "Para desfazer: cp $BK app.py && sudo systemctl restart wms-russia.service"
