"""NF do pedido ficou PENDENTE no Bling (não foi para a SEFAZ): envia, espera autorizar, manda o XML para o ML
e diz se a etiqueta já está liberada. Uso: venv/bin/python -u enviar_nf_pedido.py <ml_id>"""
import sys
import time

import requests

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from services.bling_service import BlingService
from services.ml_service import MercadoLivreService

ml_id = sys.argv[1]
SIT = {1: "Pendente", 2: "Cancelada", 3: "Aguardando recibo", 4: "Rejeitada", 5: "Autorizada", 6: "Emitida DANFE",
       7: "Registrada", 8: "Aguardando protocolo", 9: "Denegada", 10: "Consultar situação", 11: "Bloqueada"}
b = BlingService()
pv = b._get_json("/pedidos/vendas", params={"numerosLojas[]": ml_id, "limite": 5}).get("data") or []
if not pv:
    sys.exit("PV não encontrado no Bling para esse pedido")
det = b._get_json(f"/pedidos/vendas/{pv[0]['id']}").get("data") or {}
nf_id = (det.get("notaFiscal") or {}).get("id")
if not nf_id:
    sys.exit(f"PV {det.get('numero')} sem NF ligada no Bling")
nf = b._get_json(f"/nfe/{nf_id}").get("data") or {}
sit = nf.get("situacao")
print(f"NF {nf.get('numero')} | situação {sit} ({SIT.get(sit, '?')})")

if sit not in (5, 6, 7):
    r = b._post(f"/nfe/{nf_id}/enviar")
    print(f"Enviar para a SEFAZ: HTTP {r.status_code} {r.text[:400]}")
    for _ in range(30):
        time.sleep(3)
        nf = b._get_json(f"/nfe/{nf_id}").get("data") or {}
        sit = nf.get("situacao")
        print(f"   ... situação {sit} ({SIT.get(sit, '?')})")
        if sit in (2, 4, 5, 6, 7, 9) or nf.get("chaveAcesso"):
            break
    if sit not in (5, 6, 7):
        sys.exit(f"NÃO AUTORIZOU: {nf.get('xMotivo') or SIT.get(sit) or sit}. Abra a NF {nf.get('numero')} no Bling para ver o motivo.")
print(f"AUTORIZADA: NF {nf.get('numero')} chave {nf.get('chaveAcesso')}")

# XML para o ML (o envio só libera a etiqueta da coleta/agência com a NF)
ml = MercadoLivreService()
ro = ml._request("GET", f"/orders/{ml_id}")
ship = (ro.json().get("shipping") or {}).get("id") if ro is not None and ro.status_code == 200 else None
if not ship:
    rp = ml._request("GET", f"/packs/{ml_id}")
    if rp is not None and rp.status_code == 200:
        ship = (rp.json().get("shipment") or {}).get("id")
xml_url = nf.get("xml") or (b._get_json(f"/nfe/{nf_id}").get("data") or {}).get("xml")
if ship and xml_url:
    x = requests.get(xml_url, timeout=20)
    if x.status_code == 200 and x.content[:5] == b"<?xml":
        ri = requests.post(f"https://api.mercadolibre.com/shipments/{ship}/invoice_data/?siteId=MLB",
                           headers={"Authorization": f"Bearer {ml.access_token}", "Content-Type": "application/xml"},
                           data=x.content, timeout=30)
        print(f"XML para o ML: HTTP {ri.status_code} {ri.text[:200]}")
    else:
        print("não consegui baixar o XML da NF no Bling")
if ship:
    time.sleep(3)
    rs = ml._request("GET", f"/shipments/{ship}")
    if rs is not None and rs.status_code == 200:
        print(f"Envio {ship}: {rs.json().get('status')} / {rs.json().get('substatus')}")
    print("Agora é só ir em 'Já saíram hoje' na Expedição e tocar em reimprimir a etiqueta (vem do ML).")
