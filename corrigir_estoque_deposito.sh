#!/usr/bin/env bash
# Faz o WMS mostrar o estoque SÓ do depósito da LHVMed (BLING_DEPOSITO_ID), e não a soma de todos os
# depósitos (que incluía FULL Mercado Livre negativo). Rode DENTRO da pasta do backend da LHVMed. Idempotente, com backup.
set -e
[ -f app.py ] || { echo "ERRO: rode dentro da pasta do backend (onde esta o app.py)"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed (RussianL). Pasta atual: $PWD"; exit 1;; esac
python3 - <<'PYEOF'
f = "services/bling_service.py"
s = open(f, encoding="utf-8").read()
if "# estoque so do deposito local" in s:
    print(">> bling_service.py ja estava corrigido (ok)"); raise SystemExit(0)
ancora = """                if isinstance(depositos, list) and depositos:
                    soma = 0
                    for d in depositos:"""
novo = """                if isinstance(depositos, list) and depositos:
                    # estoque so do deposito local: ignora FULL ML/B2W/Amazon (podem estar negativos)
                    _dep = (os.getenv("BLING_DEPOSITO_ID") or "").strip()
                    if _dep:
                        depositos = [d for d in depositos if isinstance(d, dict) and str(d.get("id")) == _dep]
                    soma = 0
                    for d in depositos:"""
if s.count(ancora) != 1:
    print("!! Nao achei o trecho esperado (ou ha mais de um). Nada foi alterado."); raise SystemExit(1)
open(f + ".bak_dep", "w", encoding="utf-8").write(s)
open(f, "w", encoding="utf-8").write(s.replace(ancora, novo, 1))
print(">> bling_service.py corrigido (backup em services/bling_service.py.bak_dep)")
PYEOF
python3 -m py_compile services/bling_service.py && echo COMPILOU
echo "Reinicie: sudo systemctl restart wms-russia.service. Exige BLING_DEPOSITO_ID no servico (ja esta no nao_achado.conf)."
echo "Desfazer: cp services/bling_service.py.bak_dep services/bling_service.py && sudo systemctl restart wms-russia.service"
