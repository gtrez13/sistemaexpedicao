"""SÓ LEITURA (não grava nada no WMS, no Bling nem no ML). Confere a sincronização da Separação:
  1) Mercado Livre: cada pedido ML aberto no WMS -> dia em que TEM que sair (mesma regra da Saída: limite de despacho
     do ML / SLA do Flex). Mostra os que NÃO são de hoje (não deviam estar na separação).
  2) Comercial: tudo 'Em andamento' no Bling (situação 15, últimos 120 dias) x o que está no WMS.
  3) Copia o código de produção da sincronização para o relatório (para ajustar sem chutar).
Uso: venv/bin/python -u sonda_sync.py      -> mostra o resumo e grava sonda_sync_<hora>.txt (mande esse arquivo)"""
import inspect
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import mapa_routes as m
from db import db_session

ARQ = f"sonda_sync_{time.strftime('%Y%m%d_%H%M')}.txt"
saida = open(ARQ, "w", encoding="utf-8")


def p(*a):
    t = " ".join(str(x) for x in a)
    print(t)
    saida.write(t + "\n")


def so_arquivo(t):
    saida.write(t + "\n")


hoje = m._hoje()
p(f"== SONDA SYNC · hoje (Brasília) = {hoje} · {time.strftime('%H:%M')} ==")

with db_session() as c:
    abertos = [dict(r) for r in c.execute(
        "SELECT * FROM pedidos WHERE status IN ('PENDENTE','PARCIAL','SEPARADO') ORDER BY id").fetchall()]
p(f"\nPedidos abertos no WMS (PENDENTE/PARCIAL/SEPARADO): {len(abertos)}")
por_origem = {}
for r in abertos:
    por_origem.setdefault(str(r.get("origem") or "ML").upper(), []).append(r)
p("   " + ", ".join(f"{k}: {len(v)}" for k, v in sorted(por_origem.items())))
if abertos:
    so_arquivo("   colunas da tabela pedidos: " + ", ".join(sorted(abertos[0].keys())))

# ── 1) Mercado Livre ──
p("\n== 1) MERCADO LIVRE: dia em que cada pedido aberto TEM que sair ==")
ml_abertos = [r for r in abertos if str(r.get("origem") or "ML").upper() == "ML"]
from services.ml_service import MercadoLivreService
ml = MercadoLivreService()


def dia(r):
    sid = r.get("ml_shipping_id")
    st, sub, lim = m._status_envio_ml(ml, sid) if sid else (None, None, None)
    return r, st, sub, lim


grupos = {"hoje": [], "amanha_ou_depois": [], "atrasado": [], "sem_data": []}
with ThreadPoolExecutor(6) as ex:
    for r, st, sub, lim in ex.map(dia, ml_abertos):
        g = "sem_data" if not lim else "hoje" if lim == hoje else "amanha_ou_depois" if lim > hoje else "atrasado"
        grupos[g].append((r, st, sub, lim))
for g, nome in (("hoje", "SAI HOJE (certo)"), ("amanha_ou_depois", "NÃO É DE HOJE: amanhã ou depois"),
                ("atrasado", "dia anterior (atrasado)"), ("sem_data", "sem data no ML")):
    p(f"   {nome}: {len(grupos[g])}")
    if g != "hoje":
        for r, st, sub, lim in grupos[g][:40]:
            p(f"      #{r.get('ml_id')} {str(r.get('cliente_nome') or '')[:28]:28} sai {lim or '?'} · ML {st}/{sub} · {r.get('logistic_type')} · "
              f"WMS {r.get('status')} · entrou {str(r.get('criado_em') or '')[:16]}")

# ── 2) Comercial ──
p("\n== 2) COMERCIAL: 'Em andamento' no Bling x WMS ==")
from services.bling_service import BlingService
b = BlingService()
bling = []
ini = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
for pag in range(1, 11):
    try:
        d = b._get_json("/pedidos/vendas", params={"pagina": pag, "limite": 100, "idsSituacoes[]": [15],
                                                    "dataInicial": ini, "dataFinal": date.today().strftime("%Y-%m-%d")}, timeout=(10, 30))
    except Exception as e:
        p(f"   erro lendo o Bling (página {pag}): {e}")
        break
    itens = d.get("data") or []
    bling += itens
    if len(itens) < 100:
        break
    time.sleep(0.4)
p(f"   Em andamento no Bling (desde {ini}): {len(bling)}")
com_wms = por_origem.get("COMERCIAL", [])
with db_session() as c:
    todos_com = [dict(r) for r in c.execute("SELECT * FROM pedidos WHERE origem='COMERCIAL' ORDER BY id DESC LIMIT 400").fetchall()]


def chaves_wms(r):
    out = set()
    for k in ("ml_id", "bling_numero", "bling_pedido_venda_id", "bling_id", "pedido_bling", "numero"):
        v = str(r.get(k) or "").strip()
        if v:
            out.add(v)
            out.add(re.sub(r"\D", "", v))
    return out - {""}


idx = {}
for r in todos_com:
    for k in chaves_wms(r):
        idx.setdefault(k, r)
falta, ok = [], []
for pv in bling:
    numero, pid = str(pv.get("numero") or ""), str(pv.get("id") or "")
    r = idx.get(numero) or idx.get(pid)
    loja = (pv.get("loja") or {}).get("id")
    (ok if r else falta).append((pv, r, loja))
p(f"   desses, no WMS: {len(ok)} · FALTANDO no WMS: {len(falta)}")
for pv, r, loja in falta[:60]:
    p(f"      PV {pv.get('numero')} · {pv.get('data')} · {str((pv.get('contato') or {}).get('nome') or '')[:32]} · loja {loja} · "
      f"nº loja {pv.get('numeroLoja') or '-'}")
abertos_bling = {str(pv.get("numero")) for pv in bling} | {str(pv.get("id")) for pv in bling}
sobra = [r for r in com_wms if not (chaves_wms(r) & abertos_bling)]
p(f"   Comercial aberto no WMS que NÃO está mais 'Em andamento' no Bling: {len(sobra)}")
for r in sobra[:40]:
    p(f"      #{r.get('ml_id')} · {str(r.get('cliente_nome') or '')[:32]} · WMS {r.get('status')} · entrou {str(r.get('criado_em') or '')[:16]}")
if hasattr(b, "buscar_vendas_internas"):
    for dias in (7, 120):
        try:
            v = b.buscar_vendas_internas(dias=dias)
            p(f"   o que o botão do Comercial traria com dias={dias}: {len(v)} pedido(s)")
        except Exception as e:
            p(f"   buscar_vendas_internas(dias={dias}) falhou: {e}")

# ── 3) código de produção da sincronização (só no arquivo) ──
so_arquivo("\n== 3) CÓDIGO DE PRODUÇÃO (para conferir a regra) ==")
mascara = lambda t: re.sub(r"(APP_USR-[\w-]+|[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-]{40,})", "***", t)
for nome_mod in ("api_routes", "routes"):
    try:
        mod = __import__(nome_mod)
        src = inspect.getsource(mod)
    except Exception as e:
        so_arquivo(f"[{nome_mod}] não li: {e}")
        continue
    for rota in ("/sincronizar", "/sync/vendas-internas", "/sync/shopee", "/pedidos-status", "/pedidos/status"):
        for mm in re.finditer(r"^@[\w.]+\.(?:route|get|post)\([\"']%s[\"'].*$" % re.escape(rota), src, flags=re.M):
            ini_f = mm.start()
            fim_f = re.search(r"^@[\w.]+\.(?:route|get|post)\(", src[mm.end():], flags=re.M)
            trecho = src[ini_f: mm.end() + (fim_f.start() if fim_f else 6000)][:9000]
            so_arquivo(f"\n----- {nome_mod}.py {rota} -----\n" + mascara(trecho))
try:
    from services import ml_service as mls
    for fn in ("buscar_cards", "_fetch_orders_ready_to_ship", "_best_ship_datetime"):
        obj = getattr(mls.MercadoLivreService, fn, None) or getattr(mls, fn, None)
        if obj:
            so_arquivo(f"\n----- ml_service.{fn} -----\n" + mascara(inspect.getsource(obj))[:9000])
except Exception as e:
    so_arquivo(f"[ml_service] não li: {e}")
try:
    from services import bling_service as bs
    obj = getattr(bs.BlingService, "buscar_vendas_internas", None)
    if obj:
        so_arquivo("\n----- bling_service.buscar_vendas_internas -----\n" + mascara(inspect.getsource(obj))[:9000])
except Exception as e:
    so_arquivo(f"[bling_service] não li: {e}")

saida.close()
print(f"\nRelatório completo em: {ARQ}  (mande esse arquivo)")
print("Nada foi criado, alterado ou apagado.")
