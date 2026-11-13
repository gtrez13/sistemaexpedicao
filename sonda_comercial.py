"""SÓ LEITURA. Para cada pedido Comercial aberto no WMS (PENDENTE/PARCIAL/SEPARADO): em que situação ele está no Bling agora.
Uso: venv/bin/python -u sonda_comercial.py"""
import re
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from db import db_session
from services.bling_service import BlingService

b = BlingService()
nomes = {}


def nome_sit(sid):
    if sid in nomes:
        return nomes[sid]
    try:
        d = b._get_json(f"/situacoes/{sid}", timeout=(10, 30))
        nomes[sid] = ((d or {}).get("data") or {}).get("nome") or "?"
    except Exception:
        nomes[sid] = "?"
    return nomes[sid]


with db_session() as c:
    rows = [dict(r) for r in c.execute("SELECT * FROM pedidos WHERE origem='COMERCIAL' AND status IN ('PENDENTE','PARCIAL','SEPARADO') ORDER BY id").fetchall()]
print(f"Comercial aberto no WMS: {len(rows)}\n")
for r in rows:
    pid = re.sub(r"\D", "", str(r.get("ml_id") or ""))
    try:
        d = (b._get_json(f"/pedidos/vendas/{pid}", timeout=(10, 30)) or {}).get("data") or {}
    except Exception as e:
        print(f"#{r['ml_id']} erro: {e}")
        continue
    sit = d.get("situacao") or {}
    print(f"PV {d.get('numero')} · {str((d.get('contato') or {}).get('nome') or '')[:30]:30} · Bling: {sit.get('id')} {nome_sit(sit.get('id'))} · "
          f"WMS {r.get('status')} · data {d.get('data')} · loja {(d.get('loja') or {}).get('id')}")
    time.sleep(0.35)
print("\nNada foi alterado.")
