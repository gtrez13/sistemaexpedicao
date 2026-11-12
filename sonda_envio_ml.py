"""SÓ LEITURA. Mostra que DIA o Mercado Livre diz que um pacote tem que sair e o que a Saída do WMS decide.
Uso: venv/bin/python -u sonda_envio_ml.py <nº da venda | nº do pacote | nº do envio>"""
import json
import re
import sys
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from services.ml_service import MercadoLivreService
import mapa_routes as m
from db import db_session

cod = re.sub(r"\D", "", sys.argv[1] if len(sys.argv) > 1 else "")
if not cod:
    sys.exit("Passe o nº da venda, do pacote ou do envio.")
ml = MercadoLivreService()


def get(caminho):
    r = ml._request("GET", caminho)
    if r is None:
        return 0, None
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:300]


print(f"Hora do servidor: {time.strftime('%d/%m %H:%M', time.localtime())} (fuso do servidor) · Brasília: hoje é {m._hoje()}")

sid, orders = None, []
st, o = get(f"/orders/{cod}")
if st == 200 and isinstance(o, dict):
    orders = [o]
    sid = str((o.get("shipping") or {}).get("id") or "")
else:
    st, pk = get(f"/packs/{cod}")
    if st == 200 and isinstance(pk, dict):
        sid = str((pk.get("shipment") or {}).get("id") or "")
        for x in pk.get("orders") or []:
            s2, o2 = get(f"/orders/{x.get('id')}")
            if s2 == 200:
                orders.append(o2)
    else:
        sid = cod
for o in orders:
    print(f"venda {o.get('id')} · pacote {o.get('pack_id') or '-'} · {o.get('status')} · comprada {str(o.get('date_created'))[:16]} · "
          f"comprador {(o.get('buyer') or {}).get('nickname')}")

st, j = get(f"/shipments/{sid}")
if st != 200 or not isinstance(j, dict):
    sys.exit(f"Envio {sid}: o ML respondeu {st} {str(j)[:200]}")
print(f"\nenvio {sid} · {j.get('status')}/{j.get('substatus')} · logística {j.get('logistic_type') or (j.get('logistic') or {}).get('type')} · "
      f"recebe {(j.get('receiver_address') or {}).get('receiver_name')}")


def datas(titulo, d):
    if not isinstance(d, dict):
        return
    achou = {k: v.get("date") for k, v in d.items() if isinstance(v, dict) and v.get("date")}
    for k, v in d.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict) and v2.get("date"):
                    achou[f"{k}.{k2}"] = v2["date"]
    print(f"\n== {titulo} ==")
    for k, v in sorted(achou.items()):
        print(f"   {k:42} {v}")
    if not achou:
        print("   (nenhuma data)")


datas("datas no envio (lead_time)", j.get("lead_time"))
datas("datas no envio (shipping_option)", j.get("shipping_option"))
s2, lt = get(f"/shipments/{sid}/lead_time")
print(f"\n/shipments/{sid}/lead_time -> HTTP {s2}")
datas("datas em /lead_time", lt if isinstance(lt, dict) else {})
s3, sla = get(f"/shipments/{sid}/sla")
print(f"\n/shipments/{sid}/sla -> HTTP {s3} {json.dumps(sla, ensure_ascii=False)[:300] if sla else ''}")

m._desp_cache.pop(str(sid), None)
dia, fonte = m._data_despacho(ml, sid, j)
hoje = m._hoje()
print(f"\n== O que a Saída decide ==")
print(f"   tem que sair: {dia or 'SEM DATA'}  (pela {fonte or '-'})")
with db_session() as c:
    m._saida_tabelas(c)
    ids = [str(o.get("id")) for o in orders] + [str(o.get("pack_id") or "") for o in orders] + [str(sid)]
    ad = None
    for k in [x for x in ids if x]:
        ad = ad or c.execute("SELECT * FROM saida_adiados WHERE ml_id=?", (k,)).fetchone()
    bp = None
    for k in [x for x in ids if x]:
        bp = bp or c.execute("SELECT * FROM saida_bipes WHERE ml_id=?", (k,)).fetchone()
if ad and ad["dia"] > hoje:
    print(f"   => tirado de hoje por {ad['por']}: sai {ad['dia']} · NÃO aparece como de hoje")
elif not dia:
    print("   => o ML não deu o dia: aparece em 'Sem data no ML — confira', NÃO conta como de hoje")
elif dia > hoje:
    print(f"   => é de OUTRO DIA ({dia}): não conta hoje, e o bip recusa")
elif dia < hoje:
    print(f"   => ATRASADO (era para {dia}): conta como de hoje")
else:
    print("   => é de HOJE")
if bp:
    print(f"   bipado em {m._dia_ts(bp['em'])} por {bp['por']}" + (" (hoje: fica como carregado — use Desfazer ou 'Não é de hoje')" if m._dia_ts(bp['em']) == hoje else ""))
print("\nNada foi criado nem alterado.")
