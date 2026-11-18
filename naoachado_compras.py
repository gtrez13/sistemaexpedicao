#!/usr/bin/env python3
"""
"Não encontrei" -> StockMind (compras.lhvmed.com.br): manda para o comprador os produtos zerados que ainda não foram
avisados (os de antes da integração, ou os que deram erro). Sem --enviar só mostra.

    venv/bin/python -u naoachado_compras.py            prévia (não manda nada)
    venv/bin/python -u naoachado_compras.py --enviar   manda

Só vai quem foi zerado no Bling (ou já estava sem saldo) e NÃO foi achado depois.
"""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
if not os.path.exists("app.py"):
    sys.exit("Rode dentro da pasta do backend (onde fica o app.py).")

import naoachado_routes as na
from db import db_session

enviar = "--enviar" in sys.argv
na.garantir_tabelas()
with db_session() as c:
    rows = c.execute(
        "SELECT * FROM nao_achado WHERE estoque_status IN ('ok','ja_zerado') "
        "AND COALESCE(compras_status,'') NOT IN ('ok','achado') ORDER BY id").fetchall()
cfg = bool(os.environ.get("COMPRAS_API_URL") and os.environ.get("COMPRAS_TOKEN"))
print(f"{len(rows)} produto(s) zerado(s) sem aviso ao comprador. StockMind {'configurado' if cfg else 'NÃO configurado (COMPRAS_API_URL/COMPRAS_TOKEN)'}.")
for r in rows:
    print(f"  #{r['id']}  {r['sku']}  {(r['nome'] or '')[:50]}  tinha {r['estoque_anterior']}  ({r['estoque_status']}, aviso: {r['compras_status'] or 'nunca'})")
if not enviar:
    print("\nPrévia: nada foi enviado. Para mandar: venv/bin/python -u naoachado_compras.py --enviar")
    sys.exit(0)
if not cfg:
    sys.exit("COMPRAS_API_URL/COMPRAS_TOKEN não estão no ambiente: rode com as variáveis do serviço (veja o ligar_compras.sh).")
ok = 0
for r in rows:
    st, msg = na.avisar_compras("estoque_zerado", r, r["estoque_anterior"], r["explicacao"] or "")
    with db_session() as c:
        c.execute("UPDATE nao_achado SET compras_status=?, compras_msg=? WHERE id=?", (st, (msg or "")[:300], r["id"]))
    ok += st == "ok"
    print(f"  #{r['id']} {r['sku']}: {st} — {msg}")
print(f"\n{ok} de {len(rows)} avisado(s).")
