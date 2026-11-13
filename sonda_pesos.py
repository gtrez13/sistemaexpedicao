"""SÓ LEITURA. Peso por unidade (cadastro do Bling) dos produtos que a LHVMed recebe e vende, para escolher a balança.
Uso: venv/bin/python -u sonda_pesos.py          (uns 3 a 5 minutos; não muda nada no Bling nem no WMS)"""
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
try:
    from db import db_session
except ImportError:
    from db.database import db_session
from services.bling_service import BlingService

MAX_SKUS = 500
DIVISOES = [1, 2, 5, 10, 50]        # g; 50 = a balança de plataforma que já existe
saida = []


def p(s=""):
    print(s)
    saida.append(s)


def get(b, path, params=None):
    for t in range(5):
        try:
            return b._get_json(path, params=params, timeout=(10, 40)) or {}
        except Exception as e:
            if "429" in str(e):
                time.sleep(2 + 2 * t)
                continue
            raise
    return {}


# 1) SKUs que importam: vendidos nos últimos 120 dias (com quantas unidades) + os que já passaram no recebimento
desde = time.strftime("%Y-%m-%d", time.localtime(time.time() - 120 * 86400))
vendas = {}
with db_session() as c:
    for r in c.execute("SELECT pr.sku AS sku, MAX(pr.nome) AS nome, SUM(pp.quantidade) AS q FROM pedido_produtos pp "
                       "JOIN produtos pr ON pr.id = pp.produto_id JOIN pedidos pe ON pe.id = pp.pedido_id "
                       "WHERE pe.criado_em >= ? GROUP BY pr.sku", (desde,)).fetchall():
        vendas[str(r["sku"]).strip()] = {"nome": r["nome"], "q": int(r["q"] or 0)}
    try:
        for r in c.execute("SELECT DISTINCT sku FROM receb_itens WHERE sku IS NOT NULL AND sku <> ''").fetchall():
            vendas.setdefault(str(r["sku"]).strip(), {"nome": None, "q": 0})
    except Exception:
        pass
skus = sorted(vendas, key=lambda s: -vendas[s]["q"])[:MAX_SKUS]
p(f"Produtos a consultar: {len(skus)} (vendidos desde {desde} + recebidos)")

# 2) Bling: código -> id (50 por chamada), depois o peso de cada um
b = BlingService()
ids = {}
for i in range(0, len(skus), 50):
    lote = skus[i:i + 50]
    d = get(b, "/produtos", {"codigos[]": lote, "limite": 100, "criterio": 5})
    for it in d.get("data") or []:
        cod = str(it.get("codigo") or "").strip()
        if cod in vendas and it.get("id"):
            ids.setdefault(cod, it["id"])
    time.sleep(0.4)
p(f"Achados no Bling: {len(ids)}")

linhas = []
for n, (sku, pid) in enumerate(ids.items(), 1):
    try:
        d = (get(b, f"/produtos/{pid}") or {}).get("data") or {}
    except Exception as e:
        p(f"   {sku}: erro {e}")
        continue
    pl, pb = float(d.get("pesoLiquido") or 0), float(d.get("pesoBruto") or 0)
    g = round((pl or pb) * 1000, 1)
    linhas.append({"sku": sku, "nome": (d.get("nome") or vendas[sku]["nome"] or "")[:55], "g": g, "q": vendas[sku]["q"]})
    if n % 50 == 0:
        print(f"   ... {n}/{len(ids)}")
    time.sleep(0.35)

com = [x for x in linhas if x["g"] > 0]
sem = [x for x in linhas if x["g"] <= 0]
tot_q = sum(x["q"] for x in com) or 1
p(f"\nCom peso cadastrado: {len(com)}   sem peso: {len(sem)}")

# 3) faixas de peso
faixas = [(0, 10), (10, 25), (25, 50), (50, 100), (100, 250), (250, 1000), (1000, 10 ** 9)]
p("\nPeso de 1 unidade        produtos   % das unidades vendidas")
for a, z in faixas:
    xs = [x for x in com if a <= x["g"] < z]
    nome = f"{a} a {z} g" if z < 10 ** 9 else f"acima de {a} g"
    p(f"   {nome:22} {len(xs):6}     {100 * sum(x['q'] for x in xs) / tot_q:5.1f}%")

# 4) que divisão de balança confere cada produto (1 unidade tem que valer pelo menos 5 divisões)
p("\nBalança que muda de X em X gramas confere:")
for dv in DIVISOES:
    ok = [x for x in com if x["g"] >= 5 * dv]
    extra = "   <- a de plataforma que vocês já têm" if dv == 50 else ""
    p(f"   {dv:>3} g: {len(ok):4} de {len(com)} produtos ({100 * sum(x['q'] for x in ok) / tot_q:5.1f}% das unidades){extra}")

# 5) mais leves e mais pesados
p("\nOs 15 mais leves:")
for x in sorted(com, key=lambda x: x["g"])[:15]:
    p(f"   {x['g']:>8} g  {x['sku']:18} {x['nome']}")
p("\nOs 10 mais pesados:")
for x in sorted(com, key=lambda x: -x["g"])[:10]:
    p(f"   {x['g']:>8} g  {x['sku']:18} {x['nome']}")
p("\nOs 15 mais vendidos sem peso no Bling:")
for x in sorted(sem, key=lambda x: -x["q"])[:15]:
    p(f"   {x['sku']:18} {x['q']:>5} un  {x['nome']}")

arq = f"sonda_pesos_{time.strftime('%Y%m%d_%H%M')}.txt"
open(arq, "w", encoding="utf-8").write("\n".join(saida) + "\n")
p(f"\nNada foi alterado. Relatório: {arq}")
