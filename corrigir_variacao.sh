#!/usr/bin/env bash
# Pedido do ML com variação (ex.: cor): usa o SKU FILHO que vem no próprio pedido, não o SKU mãe do anúncio.
# Mostra antes o que o ML manda nos pedidos da tela 1 (só leitura). Backup + 1 comando para desfazer.
set -e
[ -f app.py ] || { echo "rode dentro da pasta do backend"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "isso é só para o LHVMed (RussianL)"; exit 1;; esac

echo "== Pedidos do ML na tela 1: o que o ML manda x o que o WMS gravou =="
venv/bin/python -u - <<'PY' || echo "(diagnóstico falhou, sigo com a correção)"
from dotenv import load_dotenv; load_dotenv(".env")
from db import db_session
from services.ml_service import MercadoLivreService
ml = MercadoLivreService()
with db_session() as c:
    rows = c.execute("SELECT pe.ml_id, pr.sku, pr.nome FROM pedidos pe JOIN pedido_produtos pp ON pp.pedido_id=pe.id "
                     "JOIN produtos pr ON pr.id=pp.produto_id WHERE pe.status IN ('PENDENTE','PARCIAL') "
                     "AND COALESCE(pe.origem,'ML')='ML' ORDER BY pe.ml_id").fetchall()
vistos = set()
for r in rows:
    if r["ml_id"] in vistos: continue
    vistos.add(r["ml_id"])
    resp = ml._request("GET", f"/orders/{r['ml_id']}")
    if not resp or resp.status_code != 200: continue
    for oi in resp.json().get("order_items") or []:
        it = oi.get("item") or {}
        if it.get("variation_id") or it.get("variation_attributes"):
            cor = ", ".join(f"{a.get('name')}: {a.get('value_name')}" for a in it.get("variation_attributes") or [])
            gravou = [(x["sku"], x["nome"]) for x in rows if x["ml_id"] == r["ml_id"]]
            print(f"pedido {r['ml_id']} | {it.get('title')} [{cor}] | variação {it.get('variation_id')} | "
                  f"SKU no pedido: {it.get('seller_sku')} / {it.get('seller_custom_field')} | WMS gravou: {gravou}")
PY

F=services/ml_service.py
BK="$F.bak_variacao_$(date +%Y%m%d%H%M%S)"
cp "$F" "$BK"
venv/bin/python - "$F" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
if "SKU_FILHO_DO_PEDIDO" in s:
    print("já estava corrigido"); sys.exit(0)
m = re.search(r'\n([ \t]+)if var_sku:\n[ \t]+oi\["seller_sku"\] = var_sku\n([ \t]+)elif real_sku:', s)
if not m:
    print("NÃO achei o trecho do SKU da variação — nada mudou. Linhas com var_sku:")
    for i, l in enumerate(s.splitlines(), 1):
        if "var_sku" in l or "real_sku" in l: print(i, l)
    sys.exit(1)
ind = m.group(1)
novo = (f'\n{ind}# SKU_FILHO_DO_PEDIDO: o próprio pedido traz o SKU do que foi vendido (filho, se tiver variação);\n'
        f'{ind}# o SKU do anúncio (real_sku) é o da MÃE e só vale se o pedido não trouxer nenhum\n'
        f'{ind}_it = oi.get("item") or {{}}\n'
        f'{ind}sku_pedido = str(_it.get("seller_sku") or _it.get("seller_custom_field") or "").strip()\n'
        f'{ind}if var_sku:\n{ind}    oi["seller_sku"] = var_sku\n'
        f'{ind}elif sku_pedido:\n{ind}    oi["seller_sku"] = sku_pedido\n'
        f'{m.group(2)}elif real_sku:')
s = s[:m.start()] + novo + s[m.end():]
open(p, "w", encoding="utf-8").write(s)
print("corrigido: usa o SKU filho que vem no pedido")
PY
venv/bin/python -m py_compile "$F"
sudo systemctl restart wms-russia.service
sleep 3
echo "OK — variação agora usa o SKU filho ($(systemctl is-active wms-russia.service))."
echo "Agora toque em 'Só atualizar pedidos' (ou 'Começar o dia') para refazer os pedidos pendentes com o código certo."
echo "Para desfazer: cp $BK $F && sudo systemctl restart wms-russia.service"
