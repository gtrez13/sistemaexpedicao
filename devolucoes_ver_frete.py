"""Mostra de onde vem a 'Taxa de devolução' (frete de volta) de uma devolução — só leitura.
Uso: venv/bin/python -u devolucoes_ver_frete.py <nome do cliente | id da devolução>"""
import json
import os
import sys

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import devolucoes_routes as d
from db import db_session

alvo = " ".join(sys.argv[1:]).strip()
with db_session() as c:
    if alvo.isdigit() and len(alvo) < 8:
        rows = c.execute("SELECT id, cliente, ml_claim_id, numero_venda, custo_ml, custo_devolucao FROM devolucoes WHERE id=?", (int(alvo),)).fetchall()
    else:
        rows = c.execute("SELECT id, cliente, ml_claim_id, numero_venda, custo_ml, custo_devolucao FROM devolucoes WHERE LOWER(cliente) LIKE ? ORDER BY id DESC LIMIT 3",
                         (f"%{alvo.lower()}%",)).fetchall()
if not rows:
    sys.exit("não achei essa devolução")
seller = str(os.environ.get("ML_SELLER_ID", ""))
for r in rows:
    print(f"\n#{r['id']} {r['cliente']} | venda {r['numero_venda']} | reclamação {r['ml_claim_id']} | custo_ml gravado: {r['custo_ml']} | taxa na prancheta: {r['custo_devolucao']}")
    if not r["ml_claim_id"]:
        continue
    st, ret = d._ml_req("GET", f"/post-purchase/v2/claims/{r['ml_claim_id']}/returns")
    print(f"  devolução no ML: {st} status={ret.get('status') if isinstance(ret, dict) else ret} | dinheiro: {ret.get('status_money') if isinstance(ret, dict) else ''}")
    for sh in (ret.get("shipments") or []) if isinstance(ret, dict) else []:
        print(f"  envio de volta {sh.get('shipment_id')} tipo={sh.get('type')} status={sh.get('status')} destino={(sh.get('destination') or {}).get('name')}")
        if not sh.get("shipment_id"):
            continue
        s2, cs = d._ml_req("GET", f"/shipments/{sh['shipment_id']}/costs")
        if s2 != 200:
            print("   custos:", s2, str(cs)[:200]); continue
        rc, sd = cs.get("receiver") or {}, cs.get("senders") or []
        quem = "NÓS (vendedor)" if str(rc.get("user_id")) == seller else rc.get("user_id")
        print(f"   quem recebe/paga a volta: {quem} | custo cobrado: R$ {rc.get('cost')} | custo cheio do frete: R$ {cs.get('gross_amount')}")
        for x in rc.get("discounts") or []:
            print(f"   desconto: {x.get('type')} R$ {x.get('promoted_amount')} / taxa {x.get('rate')}")
        for x in sd:
            print(f"   comprador (quem envia): user {x.get('user_id')} paga R$ {x.get('cost')}")
        print("   bruto do ML:", json.dumps(cs, ensure_ascii=False)[:600])
