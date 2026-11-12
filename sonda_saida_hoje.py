"""SÓ LEITURA. Raio-x da Saída de HOJE de um carro: o que foi bipado, o que foi recusado (e por quê), os fechamentos,
e cada envio do Mercado Livre desse carro com TODAS as datas que o ML manda e o dia que a Saída decide.
Uso: venv/bin/python -u sonda_saida_hoje.py FLEX      (ou COLETA)"""
import calendar
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import mapa_routes as m
from db import db_session
from services.ml_service import MercadoLivreService

carro = (sys.argv[1] if len(sys.argv) > 1 else "FLEX").upper()
hoje = m._hoje()
ini = calendar.timegm(time.strptime(hoje, "%Y-%m-%d")) + 3 * 3600
hm = lambda t: time.strftime("%H:%M", time.gmtime(int(t or 0) - 3 * 3600))
print(f"Saída {carro} · hoje {hoje} (Brasília) · servidor marca {time.strftime('%d/%m %H:%M', time.localtime())} no fuso dele")
print(f"versão instalada: {'NOVA (limite de despacho)' if hasattr(m, '_data_despacho') else 'ANTIGA'}")

with db_session() as c:
    m._saida_tabelas(c)
    log = [dict(r) for r in c.execute("SELECT * FROM saida_log WHERE em >= ? ORDER BY id", (ini,)).fetchall()]
    bips = {r["ml_id"]: dict(r) for r in c.execute("SELECT * FROM saida_bipes WHERE em >= ?", (ini,)).fetchall()}
    try:
        fech = [dict(r) for r in c.execute("SELECT id, carro, total, carregados, faltando, por, em FROM saida_fechamentos WHERE em >= ? ORDER BY id",
                                           (ini,)).fetchall()]
    except Exception:
        fech = []

print("\n== Bipes de hoje (tela Saída), por carro ==")
cont = {}
for e in log:
    if e["acao"] in ("carregou", "repetido"):
        cont.setdefault((e["tipo"] or "?", e["carro"] or "sem carro escolhido"), set()).add(e["ml_id"])
for (t, cr), ids in sorted(cont.items()):
    vivos = [i for i in ids if i in bips]
    print(f"   pacote {t:9} bipado no carro {cr:20} {len(ids):3} ({len(vivos)} ainda bipados)")
if not cont:
    print("   NENHUM bipe hoje na tela Saída")
rec = [e for e in log if e["acao"] == "recusado" and (e["carro"] == carro or e["tipo"] == carro)]
print(f"\n== Recusados hoje no {carro}: {len(rec)} ==")
for e in rec[-25:]:
    print(f"   {hm(e['em'])} {e['por'] or '-':16} {e['cliente'] or e['codigo'] or '-':32.32} {e['detalhe']}")
print("\n== Fechamentos de hoje ==")
for f in fech:
    print(f"   #{f['id']} {hm(f['em'])} {f['carro']:9} {f['carregados']} de {f['total']} · faltando {len([x for x in (f['faltando'] or '').split(',') if x])} · {f['por']}")

print(f"\n== Mercado Livre agora: envios {carro} prontos para enviar ==")
ml = MercadoLivreService()
orders = ml._fetch_orders_ready_to_ship(limit_total=600)
por_ship = {}
for o in orders:
    sid = str((o.get("shipping") or {}).get("id") or "")
    if sid:
        por_ship.setdefault(sid, []).append(o)


def ship(sid):
    r = ml._request("GET", f"/shipments/{sid}")
    return sid, (r.json() if r is not None and r.status_code == 200 else None)


sids = list(por_ship)
if sids:
    ship(sids[0])
with ThreadPoolExecutor(6) as ex:
    js = dict(ex.map(ship, sids))


def dt(d, k):
    x = (d or {}).get(k)
    return (x.get("date") or "")[:16] if isinstance(x, dict) and x.get("date") else "-"


resumo = {}
linhas = []
for sid, j in js.items():
    if not j or j.get("status") != "ready_to_ship":
        continue
    lg = str(j.get("logistic_type") or (j.get("logistic") or {}).get("type") or "").lower()
    if "fulfillment" in lg:
        continue
    t = "FLEX" if ("self_service" in lg or "flex" in lg) else "COLETA"
    if t != carro:
        continue
    lt, so = j.get("lead_time") or {}, j.get("shipping_option") or {}
    antigo = ((lt.get("estimated_schedule_limit") or {}).get("date") or (lt.get("buffering") or {}).get("date")
              or (so.get("estimated_schedule_limit") or {}).get("date") or "")[:10] or None
    novo, fonte = m._data_despacho(ml, sid, j) if hasattr(m, "_data_despacho") else (None, "-")
    sit = "SEM DATA" if not novo else "OUTRO DIA" if novo > hoje else "ATRASADO" if novo < hoje else "HOJE"
    resumo[sit] = resumo.get(sit, 0) + 1
    o0 = por_ship[sid][0]
    b = any(k in bips for k in [str(o.get("id")) for o in por_ship[sid]] + [str(o0.get("pack_id") or "")])
    linhas.append(f"   {str(o0.get('id')):17} {((j.get('receiver_address') or {}).get('receiver_name') or '')[:24]:24} {str(j.get('substatus'))[:14]:14} "
                  f"limite {dt(lt, 'estimated_handling_limit') if dt(lt, 'estimated_handling_limit') != '-' else dt(so, 'estimated_handling_limit'):16} "
                  f"agenda {dt(lt, 'estimated_schedule_limit'):16} buffer {dt(lt, 'buffering'):16} entrega {dt(lt, 'estimated_delivery_time') if dt(lt, 'estimated_delivery_time') != '-' else dt(so, 'estimated_delivery_time'):16} "
                  f"| antes: {antigo or 'sem data'} | agora: {sit} {novo or ''} ({fonte}){' | BIPADO' if b else ''}")
for l in sorted(linhas)[:80]:
    print(l)
print(f"\n   total {len(linhas)} · " + " · ".join(f"{k}: {v}" for k, v in sorted(resumo.items())))
print("\nNada foi criado nem alterado.")
