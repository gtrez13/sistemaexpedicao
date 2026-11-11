"""
Mapa do Galpão — localização de SKU (backend, v2).

Endpoints (registrar com url_prefix="/api"):

    GET    /api/mapa/localizacoes        lista o que já foi mapeado (só banco, nunca chama o Bling)
    POST   /api/mapa/localizacao         adiciona {sku, porta_palet, posicao, nome, foto} (N:N)
    DELETE /api/mapa/localizacao/<sku>   remove o SKU (?porta_palet=&posicao= => só daquela posição)
    GET    /api/mapa/produto/<sku>       nome + foto (cache -> mapa -> Bling, nessa ordem)
    GET    /api/mapa/foto/<sku>          a IMAGEM em si (baixada do Bling e guardada em disco; link do Bling expira)
    GET    /api/mapa/status              estado do Bling para este módulo (sem segredos)

Regras de segurança contra 429 / travamento (o WMS roda com 1 worker, 1 thread):
  * Os dados ficam no banco principal do WMS (Postgres via db.db_session), não no wms.db.
  * Foto/nome consultados ficam em cache no banco (7 dias; 6h se sem foto ou não achado).
  * Este módulo NUNCA renova token: se o token estiver vencido, não chama o Bling.
  * Uma única porta de saída para o Bling (_bling_get): timeout curto, no máximo 2 chamadas
    por segundo, e ao primeiro 429/401/403 entra em cooldown (>= 15 min) sem insistir.
  * Em cooldown a resposta é rápida: {"indisponivel": true, "retry_em": <s>}.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests
from flask import Blueprint, g, jsonify, request, send_file
from flask_login import login_required   # mesma sessão do resto do WMS (/api/auth/me)

try:                                  # produção: escolhe Postgres/SQLite pelo DATABASE_URL
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

try:
    from services.bling_service import BlingService
    _bling = BlingService()
except Exception:                     # sem cliente Bling: o mapa continua funcionando
    _bling = None

mapa_routes = Blueprint("mapa_routes", __name__)

BLING_BASE = "https://api.bling.com.br/Api/v3"
VALID_PP = set(range(1, 11)) | set(range(101, 112)) | {31, 32, 51}  # Porta Palet 1..10 e Cestas 1..11 (codificadas 101..111)
VALID_POS = set(range(1, 8))          # P1..P6 (porta palet) ou fileiras F1..F7 (cesta)

MIN_INTERVALO = 0.5                   # s entre chamadas ao Bling (limite do Bling: 3/s)
COOLDOWN_LIMITE = 900                 # s parado após 429 / 401 / 403
COOLDOWN_FALHA = 120                  # s parado após erro de rede / 5xx
TIMEOUT = (3, 8)                      # (conectar, ler) — nunca segurar o único worker
TTL_ACHOU = 7 * 86400
TTL_SEM_FOTO = 6 * 3600
TTL_NAO_ACHOU = 6 * 3600

_lock = threading.Lock()
_estado = {"cooldown_ate": 0.0, "motivo": None, "ultima": 0.0}
_tabelas_ok = False


class _Indisponivel(Exception):
    def __init__(self, retry_em: int, motivo: str):
        super().__init__(motivo)
        self.retry_em = max(int(retry_em), 1)
        self.motivo = motivo


# ───────────────────────── diagnóstico de lentidão ─────────────────────────
# Registra no log (journalctl) toda requisição do WMS que passar de SLOW_S segundos.
# Com 1 worker / 1 thread, a requisição lenta é justamente a que trava todo mundo.
# Só grava método, caminho (sem query string), status e duração. Para desligar: SLOW_S = 0.
SLOW_S = 1.5
_slowlog = logging.getLogger("gunicorn.error")


@mapa_routes.before_app_request
def _slow_inicio():
    g._slow_t0 = time.perf_counter()


@mapa_routes.after_app_request
def _slow_fim(resp):
    try:
        t0 = getattr(g, "_slow_t0", None)
        if SLOW_S and t0 is not None:
            dur = time.perf_counter() - t0
            if dur >= SLOW_S:
                _slowlog.warning("[SLOW] %.1fs %s %s -> %s",
                                 dur, request.method, request.path, resp.status_code)
    except Exception:
        pass
    return resp


# ───────────────────────── banco ─────────────────────────
def _garantir_tabelas():
    global _tabelas_ok
    if _tabelas_ok:
        return
    with db_session() as conn:
        # N:N — vários SKUs por posição e o mesmo SKU em várias posições
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mapa_localizacao (
                sku_key       TEXT NOT NULL,
                sku           TEXT NOT NULL,
                nome          TEXT,
                foto          TEXT,
                porta_palet   INTEGER NOT NULL,
                posicao       INTEGER NOT NULL,
                atualizado_em BIGINT,
                PRIMARY KEY (sku_key, porta_palet, posicao)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_mapa_loc_pos ON mapa_localizacao (porta_palet, posicao)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mapa_produto_cache (
                sku_key       TEXT PRIMARY KEY,
                nome          TEXT,
                foto          TEXT,
                achou         INTEGER NOT NULL,
                consultado_em BIGINT NOT NULL
            )
        """)
    _tabelas_ok = True


def _norm(sku) -> str:
    return (sku or "").strip()[:64]


def _key(sku) -> str:
    return _norm(sku).lower()


def _foto_ok(url):
    u = (url or "").strip()
    return u[:500] if u.lower().startswith(("http://", "https://")) else None


# ───────────────────────── endpoints ─────────────────────────
@mapa_routes.get("/mapa/localizacoes")
@login_required
def listar_localizacoes():
    _garantir_tabelas()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT sku, nome, foto, porta_palet, posicao FROM mapa_localizacao "
            "ORDER BY porta_palet, posicao, sku"
        ).fetchall()
    return jsonify({"localizacoes": [
        {"sku": r["sku"], "nome": r["nome"], "foto": r["foto"],
         "porta_palet": r["porta_palet"], "posicao": r["posicao"]}
        for r in rows
    ]})


@mapa_routes.post("/mapa/localizacao")
@login_required
def salvar_localizacao():
    """Adiciona o SKU a uma posição (idempotente). Nome/foto valem para o SKU inteiro."""
    d = request.get_json(silent=True) or {}
    sku = _norm(d.get("sku"))
    nome = (d.get("nome") or "").strip()[:200]
    foto = _foto_ok(d.get("foto"))
    try:
        pp = int(d.get("porta_palet"))
        pos = int(d.get("posicao"))
    except (TypeError, ValueError):
        return jsonify({"error": "porta_palet e posicao devem ser números"}), 400
    if not sku:
        return jsonify({"error": "sku é obrigatório"}), 400
    if pp not in VALID_PP or pos not in VALID_POS or (pp < 100 and pos > 6):
        return jsonify({"error": "porta_palet 1-10, vet 31-32, refri 51 (posicao 1-6) ou cesta 101-111 (fileira 1-7)"}), 400

    _garantir_tabelas()
    key = _key(sku)
    agora = int(time.time())
    with db_session() as conn:
        if not foto:      # reaproveita foto já conhecida — só banco, nunca Bling
            p = conn.execute(
                "SELECT foto FROM mapa_localizacao WHERE sku_key=? AND foto IS NOT NULL AND foto<>'' LIMIT 1",
                (key,)).fetchone()
            foto = p["foto"] if p else None
        if not foto:
            c = conn.execute("SELECT foto FROM mapa_produto_cache WHERE sku_key=?", (key,)).fetchone()
            foto = (c["foto"] if c else None) or None
        conn.execute("""
            INSERT INTO mapa_localizacao (sku_key, sku, nome, foto, porta_palet, posicao, atualizado_em)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (sku_key, porta_palet, posicao) DO UPDATE SET
                sku=EXCLUDED.sku, nome=EXCLUDED.nome, foto=EXCLUDED.foto,
                atualizado_em=EXCLUDED.atualizado_em
        """, (key, sku, nome, foto, pp, pos, agora))
        # nome/foto são do produto: mantém iguais nas outras posições do mesmo SKU
        conn.execute("UPDATE mapa_localizacao SET nome=?, foto=? WHERE sku_key=?", (nome, foto, key))
    return jsonify({"success": True, "sku": sku, "porta_palet": pp, "posicao": pos})


@mapa_routes.delete("/mapa/localizacao/<path:sku>")
@login_required
def remover_localizacao(sku):
    """Com ?porta_palet=&posicao= remove só aquela posição; sem eles, remove o SKU de todas."""
    _garantir_tabelas()
    key = _key(sku)
    pp = request.args.get("porta_palet", type=int)
    pos = request.args.get("posicao", type=int)
    with db_session() as conn:
        if pp is not None and pos is not None:
            conn.execute("DELETE FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?",
                         (key, pp, pos))
        else:
            conn.execute("DELETE FROM mapa_localizacao WHERE sku_key=?", (key,))
    return jsonify({"success": True, "sku": sku})


@mapa_routes.get("/mapa/produto/<path:sku>")
@login_required
def produto(sku):
    sku = _norm(sku)
    key = _key(sku)
    if not key:
        return jsonify({"error": "sku é obrigatório"}), 400
    _garantir_tabelas()
    agora = int(time.time())

    with db_session() as conn:
        c = conn.execute(
            "SELECT nome, foto, achou, consultado_em FROM mapa_produto_cache WHERE sku_key=?", (key,)
        ).fetchone()
        loc = conn.execute(
            "SELECT nome, foto FROM mapa_localizacao WHERE sku_key=? LIMIT 1", (key,)
        ).fetchone()

    if c and agora - int(c["consultado_em"]) < _ttl(c):
        return jsonify({"sku": sku, "nome": c["nome"], "foto": c["foto"], "cache": True})

    try:
        nome, foto, completo = _consultar_bling(sku)
    except _Indisponivel as e:
        if loc and (loc["nome"] or loc["foto"]):          # o próprio mapa já sabe
            return jsonify({"sku": sku, "nome": loc["nome"], "foto": loc["foto"], "cache": True})
        return jsonify({"sku": sku, "nome": None, "foto": None,
                        "indisponivel": True, "retry_em": e.retry_em, "motivo": e.motivo})

    if completo:
        with db_session() as conn:
            conn.execute("""
                INSERT INTO mapa_produto_cache (sku_key, nome, foto, achou, consultado_em)
                VALUES (?,?,?,?,?)
                ON CONFLICT (sku_key) DO UPDATE SET
                    nome=EXCLUDED.nome, foto=EXCLUDED.foto, achou=EXCLUDED.achou,
                    consultado_em=EXCLUDED.consultado_em
            """, (key, nome, _foto_ok(foto), 1 if (nome or foto) else 0, agora))
    return jsonify({"sku": sku, "nome": nome, "foto": _foto_ok(foto)})


FOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fotos_cache")
FOTO_TTL = 30 * 86400            # imagem guardada vale 30 dias
FOTO_TTL_SEM = 6 * 3600          # "não tem foto / falhou": tenta de novo depois de 6h


def _baixar_img(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code == 200 and r.content and (r.headers.get("content-type") or "").startswith("image"):
            return r.content, r.headers.get("content-type")
    except Exception:
        pass
    return None, None


@mapa_routes.get("/mapa/foto/<path:sku>")
@login_required
def foto_arquivo(sku):
    """Serve a foto do produto. 1º disco; senão tenta o link guardado; se expirou, pede link novo ao Bling."""
    sku = _norm(sku)
    key = _key(sku)
    if not key:
        return ("", 404)
    os.makedirs(FOTO_DIR, exist_ok=True)
    nomearq = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
    arq, sem = os.path.join(FOTO_DIR, nomearq + ".img"), os.path.join(FOTO_DIR, nomearq + ".sem")
    agora = time.time()
    if os.path.exists(arq) and agora - os.path.getmtime(arq) < FOTO_TTL:
        return _servir(arq)
    if os.path.exists(sem) and agora - os.path.getmtime(sem) < FOTO_TTL_SEM:
        return (_servir(arq) if os.path.exists(arq) else ("", 404))

    _garantir_tabelas()
    with db_session() as conn:
        links = [r["foto"] for r in conn.execute(
            "SELECT foto FROM mapa_produto_cache WHERE sku_key=? AND foto IS NOT NULL AND foto<>'' "
            "UNION SELECT foto FROM mapa_localizacao WHERE sku_key=? AND foto IS NOT NULL AND foto<>''",
            (key, key)).fetchall()]
    dados = ctype = None
    for u in links:                                   # link guardado ainda vale?
        dados, ctype = _baixar_img(u)
        if dados:
            break
    novo = None
    if not dados:                                     # expirou: link novo do Bling
        try:
            _, novo, _ = _consultar_bling(sku)
        except _Indisponivel:
            return (_servir(arq) if os.path.exists(arq) else ("", 404))
        if novo:
            dados, ctype = _baixar_img(novo)
    if not dados:                                     # Bling sem foto: pega a 1ª foto do anúncio no Mercado Livre
        ml = _foto_ml(sku)
        if ml:
            dados, ctype = _baixar_img(ml)
    if not dados:
        open(sem, "w").close()
        return (_servir(arq) if os.path.exists(arq) else ("", 404))
    tmp = arq + ".tmp"
    with open(tmp, "wb") as f:
        f.write(dados)
    os.replace(tmp, arq)
    with open(arq + ".ct", "w") as f:
        f.write(ctype or "image/jpeg")
    if os.path.exists(sem):
        os.remove(sem)
    if novo:                                          # guarda o link novo também
        with db_session() as conn:
            conn.execute("UPDATE mapa_produto_cache SET foto=? WHERE sku_key=?", (novo, key))
            conn.execute("UPDATE mapa_localizacao SET foto=? WHERE sku_key=?", (novo, key))
    return _servir(arq)


def _ml_cred():
    tok, uid = os.environ.get("ML_ACCESS_TOKEN"), os.environ.get("ML_SELLER_ID")
    try:                                   # o token do ML é renovado no .env pelo resto do sistema: lê o atual
        from dotenv import dotenv_values
        env = dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        tok, uid = env.get("ML_ACCESS_TOKEN") or tok, env.get("ML_SELLER_ID") or uid
    except Exception:
        pass
    return tok, uid


def _foto_ml(sku: str):
    """1ª foto do anúncio do ML com esse SKU (seller_sku). Só leitura, timeout curto, nunca renova token."""
    tok, uid = _ml_cred()
    if not tok or not uid:
        return None
    h = {"Authorization": f"Bearer {tok}"}
    try:
        r = requests.get(f"https://api.mercadolibre.com/users/{uid}/items/search", params={"seller_sku": sku, "limit": 5},
                         headers=h, timeout=TIMEOUT)
        ids = (r.json().get("results") or []) if r.status_code == 200 else []
        if not ids:
            return None
        r = requests.get("https://api.mercadolibre.com/items", params={"ids": ",".join(ids[:5]), "attributes": "id,pictures,thumbnail"},
                         headers=h, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        for it in r.json() or []:
            body = it.get("body") or {}
            for pic in body.get("pictures") or []:
                u = pic.get("secure_url") or pic.get("url")
                if u:
                    return u
            if body.get("thumbnail"):
                return body["thumbnail"].replace("http://", "https://")
    except Exception as e:
        logging.getLogger(__name__).info("foto ML %s: %s", sku, e)
    return None


def _servir(arq):
    try:
        ct = open(arq + ".ct").read().strip() or "image/jpeg"
    except Exception:
        ct = "image/jpeg"
    resp = send_file(arq, mimetype=ct, max_age=86400)
    return resp


# ───────────────────────── giro (vendas) x posição ─────────────────────────
# Picking: quem sai mais tem que ficar mais perto do INÍCIO da rota. Mesma ordem da rota do celular (utils/rota.js).
def _rank(pp, p):
    pp, p = int(pp), int(p or 0)
    if pp in (51, 52):
        return 8000 + (pp - 51)                       # refrigerador: sempre o último
    if 101 <= pp <= 111:
        return 10 + (pp - 101) * 2 + p / 10           # cestas
    if pp in (31, 32):
        return 50 + (pp - 31) * 10 + p                # veterinário
    if 1 <= pp <= 5:
        return pp * 100 + p
    if 6 <= pp <= 10:
        return (pp - 5) * 100 + 20 + p
    return 9000


def _zona(pp):
    pp = int(pp)
    return "cesta" if pp > 100 else "vet" if pp in (31, 32) else "refri" if pp in (51, 52) else "pp"


def _rotulo(pp, p):
    pp = int(pp)
    if pp > 100:
        return f"Cesta {pp - 100} · F{p}"
    if pp in (31, 32):
        return f"Vet {pp - 30} · P{p}"
    if pp in (51, 52):
        return "Refrigerador"
    return f"PP{pp} · P{p}"


def _vendas_por_sku(dias):
    """{sku_key: {sku, nome, pedidos, unidades}} dos pedidos que passaram pelo WMS no período (kits já abertos)."""
    desde = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - dias * 86400))
    sql = ("SELECT LOWER(TRIM(pr.sku)) AS k, MAX(pr.sku) AS sku, MAX(pr.nome) AS nome, "
           "COUNT(DISTINCT pp.pedido_id) AS pedidos, COALESCE(SUM(pp.quantidade),0) AS unidades "
           "FROM pedido_produtos pp JOIN produtos pr ON pr.id = pp.produto_id JOIN pedidos pe ON pe.id = pp.pedido_id "
           "WHERE pe.criado_em >= ? GROUP BY LOWER(TRIM(pr.sku))")
    try:
        with db_session() as conn:
            rows = conn.execute(sql, (desde,)).fetchall()
            tot = conn.execute("SELECT COUNT(*) AS n FROM pedidos WHERE criado_em >= ?", (desde,)).fetchone()["n"]
    except Exception as e:                    # criado_em em formato estranho: usa tudo, avisando
        _slowlog.warning("giro: filtro por data falhou (%s); usando todo o histórico", e)
        with db_session() as conn:
            rows = conn.execute(sql.replace("WHERE pe.criado_em >= ? ", ""), ()).fetchall()
            tot = conn.execute("SELECT COUNT(*) AS n FROM pedidos").fetchone()["n"]
        dias = None
    return {r["k"]: {"sku": r["sku"], "nome": r["nome"], "pedidos": int(r["pedidos"] or 0),
                     "unidades": int(r["unidades"] or 0)} for r in rows if r["k"]}, int(tot or 0), dias


@mapa_routes.get("/mapa/giro")
@login_required
def giro():
    """Mais vendidos x onde estão; trocas sugeridas (só dentro da mesma zona: porta palet com porta palet,
    cesta com cesta; refrigerador e veterinário ficam onde estão); o que vende e não está no mapa."""
    _garantir_tabelas()
    dias = max(7, min(365, request.args.get("dias", default=90, type=int)))
    vendas, total_pedidos, dias_ok = _vendas_por_sku(dias)
    with db_session() as conn:
        locs = conn.execute("SELECT sku_key, sku, nome, porta_palet, posicao FROM mapa_localizacao").fetchall()

    pos_do = {}                                   # sku_key -> [loc...]
    for l in locs:
        pos_do.setdefault(l["sku_key"], []).append(
            {"pp": int(l["porta_palet"]), "p": int(l["posicao"]), "rank": _rank(l["porta_palet"], l["posicao"]),
             "label": _rotulo(l["porta_palet"], l["posicao"]), "zona": _zona(l["porta_palet"]),
             "sku": l["sku"], "nome": l["nome"]})

    # curva ABC por número de pedidos (A = 80% das saídas, B = 15%, C = resto)
    ordem = sorted(vendas.items(), key=lambda kv: (-kv[1]["pedidos"], -kv[1]["unidades"]))
    soma = sum(v["pedidos"] for _, v in ordem) or 1
    acum, classe = 0, {}
    for k, v in ordem:
        acum += v["pedidos"]
        classe[k] = "A" if acum <= soma * 0.80 or not classe else "B" if acum <= soma * 0.95 else "C"

    def info(k):
        v = vendas.get(k) or {}
        ps = sorted(pos_do.get(k, []), key=lambda x: x["rank"])
        nome = v.get("nome") or (ps[0]["nome"] if ps else None)
        return {"sku": v.get("sku") or (ps[0]["sku"] if ps else k), "nome": nome,
                "pedidos": v.get("pedidos", 0), "unidades": v.get("unidades", 0), "classe": classe.get(k, "C"),
                "posicoes": [{"pp": x["pp"], "p": x["p"], "label": x["label"]} for x in ps]}

    # trocas: o que sai muito e está longe  <->  o que quase não sai e está perto (mesma zona)
    trocas = []
    for zona in ("pp", "cesta"):
        itens = []
        for k, ps in pos_do.items():
            pz = [x for x in ps if x["zona"] == zona]
            if pz:
                best = min(pz, key=lambda x: x["rank"])
                itens.append((k, (vendas.get(k) or {}).get("pedidos", 0), best))
        usados = set()
        for k, ped, loc in sorted(itens, key=lambda t: -t[1]):
            if ped < 3 or k in usados:
                continue
            minimo = 50 if zona == "pp" else 4           # pp: outra coluna; cesta: 2 cestas mais perto
            cand = [(k2, ped2, loc2) for k2, ped2, loc2 in itens
                    if k2 not in usados and k2 != k and loc2["rank"] < loc["rank"] - minimo and ped2 * 3 <= ped]
            if not cand:
                continue
            k2, ped2, loc2 = min(cand, key=lambda t: (t[2]["rank"], t[1]))
            usados.update({k, k2})
            dif = loc["rank"] - loc2["rank"]
            perto = round(dif / 100) if zona == "pp" else round(dif / 2)
            perto_txt = (f"{perto} coluna{'s' if perto != 1 else ''}" if zona == "pp" else f"{perto} cesta{'s' if perto != 1 else ''}") + " mais perto do início"
            trocas.append({
                "zona": zona, "perto": perto_txt, "ganho": round((ped - ped2) * max(perto, 1)),
                "a": {**info(k), "de": {"pp": loc["pp"], "p": loc["p"], "label": loc["label"]},
                      "para": {"pp": loc2["pp"], "p": loc2["p"], "label": loc2["label"]}},
                "b": {**info(k2), "de": {"pp": loc2["pp"], "p": loc2["p"], "label": loc2["label"]},
                      "para": {"pp": loc["pp"], "p": loc["p"], "label": loc["label"]}},
            })
    trocas = sorted((t for t in trocas if t["ganho"] >= 10), key=lambda t: -t["ganho"])   # troca que vale o trabalho

    sem_lugar = [info(k) for k, v in ordem if v["pedidos"] >= 2 and k not in pos_do][:40]
    top = [info(k) for k, _ in ordem[:40]]
    parados = sorted((info(k) for k in pos_do if not (vendas.get(k) or {}).get("pedidos")),
                     key=lambda x: _rank(x["posicoes"][0]["pp"], x["posicoes"][0]["p"]))
    return jsonify({
        "dias": dias_ok, "pedidos": total_pedidos, "skus_vendidos": len(vendas), "skus_no_mapa": len(pos_do),
        "fonte": "pedidos que passaram pelo WMS (Mercado Livre, Shopee e Comercial), contando cada pedido uma vez por produto; kits contam pelos componentes",
        "trocas": trocas[:20], "sem_lugar": sem_lugar, "top": top, "parados": parados[:60],
    })


@mapa_routes.post("/mapa/trocar")
@login_required
def trocar():
    """Depois de trocar fisicamente: A vai para a posição de B e B para a de A (só essas duas linhas do mapa)."""
    d = request.get_json(silent=True) or {}
    try:
        a, b = d["a"], d["b"]
        ka, kb = _key(a["sku"]), _key(b["sku"])
        pa, qa, pb, qb = int(a["pp"]), int(a["p"]), int(b["pp"]), int(b["p"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "mande a:{sku,pp,p} e b:{sku,pp,p}"}), 400
    _garantir_tabelas()
    agora = int(time.time())
    with db_session() as conn:
        ra = conn.execute("SELECT * FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?", (ka, pa, qa)).fetchone()
        rb = conn.execute("SELECT * FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?", (kb, pb, qb)).fetchone()
        if not ra or not rb:
            return jsonify({"error": "o mapa mudou desde a sugestão; atualize a lista"}), 409
        conn.execute("DELETE FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?", (ka, pa, qa))
        conn.execute("DELETE FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?", (kb, pb, qb))
        for r, pp, pos in ((ra, pb, qb), (rb, pa, qa)):
            conn.execute("""
                INSERT INTO mapa_localizacao (sku_key, sku, nome, foto, porta_palet, posicao, atualizado_em)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (sku_key, porta_palet, posicao) DO UPDATE SET atualizado_em=EXCLUDED.atualizado_em
            """, (r["sku_key"], r["sku"], r["nome"], r["foto"], pp, pos, agora))
    _slowlog.info("mapa: troca %s %s-%s <-> %s %s-%s", ka, pa, qa, kb, pb, qb)
    return jsonify({"success": True})


# ───────────────────────── etiqueta direto do Mercado Livre (reimprimir) ─────────────────────────
@mapa_routes.get("/etiqueta-ml/<ml_id>")
@login_required
def etiqueta_ml(ml_id):
    """Etiqueta de envio FRESCA do ML (/shipment_labels, ZPL) para o pedido/pacote. -> {success, zpl, shipment_id, status}
    Só funciona enquanto o envio está 'ready_to_ship' (pronto para imprimir ou já impresso); depois que sai, o ML não dá mais."""
    import io, zipfile
    try:
        from services.ml_service import MercadoLivreService
        ml = MercadoLivreService()
    except Exception as e:
        return jsonify({"success": False, "error": f"ML indisponível: {e}"}), 500
    ml_id = str(ml_id).strip()
    ship = None
    try:
        with db_session() as conn:
            r = conn.execute("SELECT * FROM pedidos WHERE ml_id=?", (ml_id,)).fetchone()
            if r:
                r = dict(r)
                ship = r.get("ml_shipping_id") or None
    except Exception:
        pass
    if not ship:
        ro = ml._request("GET", f"/orders/{ml_id}")
        if ro is not None and ro.status_code == 200:
            ship = (ro.json().get("shipping") or {}).get("id")
        else:
            rp = ml._request("GET", f"/packs/{ml_id}")
            if rp is not None and rp.status_code == 200:
                ship = (rp.json().get("shipment") or {}).get("id")
                if not ship and rp.json().get("orders"):
                    ro = ml._request("GET", f"/orders/{rp.json()['orders'][0].get('id')}")
                    if ro is not None and ro.status_code == 200:
                        ship = (ro.json().get("shipping") or {}).get("id")
    if not ship:
        return jsonify({"success": False, "error": "não achei o envio desse pedido no ML"}), 404
    st, sub = None, None
    rs = ml._request("GET", f"/shipments/{ship}")
    if rs is not None and rs.status_code == 200:
        st, sub = rs.json().get("status"), rs.json().get("substatus")
    rl = ml._request("GET", "/shipment_labels", params={"shipment_ids": ship, "response_type": "zpl2"})
    if rl is None or rl.status_code != 200:
        det = ""
        try:
            det = (rl.json().get("message") or rl.json().get("error") or "") if rl is not None else "sem resposta"
        except Exception:
            det = (rl.text[:200] if rl is not None else "sem resposta")
        dica = " — o envio já saiu ou foi cancelado; o ML só dá etiqueta enquanto está pronto para envio" if st and st != "ready_to_ship" else ""
        return jsonify({"success": False, "error": f"ML não deu a etiqueta (HTTP {getattr(rl, 'status_code', '-')}, envio {st}/{sub}): {det}{dica}",
                        "shipment_id": ship, "status": st, "substatus": sub}), 422
    zpl = None
    conteudo = rl.content or b""
    if conteudo[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(conteudo))
        for n in z.namelist():
            t = z.read(n).decode("utf-8", errors="ignore")
            if "^XA" in t:
                zpl = t
                break
    elif b"^XA" in conteudo:
        zpl = conteudo.decode("utf-8", errors="ignore")
    if not zpl:
        return jsonify({"success": False, "error": "o ML respondeu, mas sem ZPL dentro", "shipment_id": ship}), 422
    _slowlog.info("etiqueta ML %s envio %s (%s/%s) %d bytes", ml_id, ship, st, sub, len(zpl))
    return jsonify({"success": True, "zpl": zpl, "shipment_id": ship, "status": st, "substatus": sub, "via": "ml"})


_com_ok = False


def _bling_svc():
    try:
        from services.bling_service import BlingService
        return BlingService()
    except Exception:
        return None


@mapa_routes.get("/comercial/info")
@login_required
def comercial_info():
    """Pedidos do Comercial em aberto no WMS -> nº do PV e TRANSPORTADORA do pedido de venda no Bling.
    Guarda em comercial_info (só pergunta ao Bling o que ainda não sabe; sem transportadora, pergunta de novo a cada 15 min)."""
    global _com_ok
    with db_session() as conn:
        if not _com_ok:
            conn.execute("CREATE TABLE IF NOT EXISTS comercial_info (ml_id TEXT PRIMARY KEY, pv_numero TEXT, "
                         "transportadora TEXT, servico TEXT, atualizado_em BIGINT)")
            _com_ok = True
        peds = [dict(r) for r in conn.execute(
            "SELECT * FROM pedidos WHERE origem='COMERCIAL' AND status IN ('PENDENTE','PARCIAL','SEPARADO')").fetchall()]
        cache = {r["ml_id"]: dict(r) for r in conn.execute("SELECT * FROM comercial_info").fetchall()}
    agora, out, b = int(time.time()), {}, None
    for p in peds:
        c = cache.get(p["ml_id"])
        if not c or (not c.get("transportadora") and agora - int(c.get("atualizado_em") or 0) > 900):
            pv = p.get("bling_pedido_venda_id")
            if pv:
                b = b or _bling_svc()
                try:
                    d = (b._get_json(f"/pedidos/vendas/{int(pv)}").get("data") or {}) if b else {}
                    t = d.get("transporte") or {}
                    vol = (t.get("volumes") or [{}])[0] or {}
                    c = {"ml_id": p["ml_id"], "pv_numero": str(d.get("numero") or p.get("bling_numero") or ""),
                         "transportadora": ((t.get("contato") or {}).get("nome") or "").strip() or None,
                         "servico": (vol.get("servico") or "").strip() or None, "atualizado_em": agora}
                    with db_session() as conn:
                        conn.execute("DELETE FROM comercial_info WHERE ml_id=?", (p["ml_id"],))
                        conn.execute("INSERT INTO comercial_info (ml_id, pv_numero, transportadora, servico, atualizado_em) VALUES (?,?,?,?,?)",
                                     (c["ml_id"], c["pv_numero"], c["transportadora"], c["servico"], agora))
                except Exception as e:
                    _slowlog.warning("comercial_info %s: %s", p["ml_id"], e)
        c = c or {}
        out[p["ml_id"]] = {"bling_numero": c.get("pv_numero") or p.get("bling_numero"),
                           "transportadora": c.get("transportadora"), "servico": c.get("servico")}
    return jsonify({"pedidos": out})


@mapa_routes.get("/comercial/pv/<ml_id>")
@login_required
def comercial_pv(ml_id):
    """Nº do pedido de venda do Bling (o que aparece na tela do Bling, não o id interno) de um pedido do Comercial."""
    with db_session() as conn:
        r = conn.execute("SELECT * FROM pedidos WHERE ml_id=?", (str(ml_id),)).fetchone()
    r = dict(r) if r else {}
    return jsonify({"numero": r.get("bling_numero"), "cliente": r.get("cliente_nome"), "id_bling": r.get("bling_pedido_venda_id")})


@mapa_routes.get("/mapa/status")
@login_required
def status():
    agora = time.time()
    exp = None
    try:
        if _bling is not None:
            exp = int(float((_bling._load_tokens() or {}).get("expires_at") or 0) - agora)
    except Exception:
        exp = None
    return jsonify({
        "bling_cooldown_s": max(int(_estado["cooldown_ate"] - agora), 0),
        "motivo": _estado["motivo"] if agora < _estado["cooldown_ate"] else None,
        "token_expira_em_s": exp,
    })


def _ttl(c) -> int:
    if not c["achou"]:
        return TTL_NAO_ACHOU
    return TTL_ACHOU if c["foto"] else TTL_SEM_FOTO


# ───────────────────────── Bling (única porta de saída) ─────────────────────────
def _cooldown(seg: int, motivo: str):
    _estado["cooldown_ate"] = time.time() + seg
    _estado["motivo"] = motivo


def _bling_get(path: str, params=None):
    """GET no Bling com todas as travas. Levanta _Indisponivel em vez de insistir."""
    with _lock:
        agora = time.time()
        if agora < _estado["cooldown_ate"]:
            raise _Indisponivel(_estado["cooldown_ate"] - agora, _estado["motivo"] or "cooldown")
        if _bling is None:
            raise _Indisponivel(300, "sem_cliente")
        try:
            t = _bling._load_tokens() or {}
        except Exception:
            t = {}
        access = t.get("access_token")
        try:
            exp = float(t.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0.0
        if not access or agora > exp - 120:
            # a renovação é do cron/WMS; o mapa nunca bate no endpoint de token
            raise _Indisponivel(300, "token_vencido")

        espera = _estado["ultima"] + MIN_INTERVALO - time.time()
        if espera > 0:
            time.sleep(espera)
        _estado["ultima"] = time.time()

        try:
            r = requests.get(BLING_BASE + path, params=params, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {access}", "Accept": "application/json",
                                     "enable-jwt": "1"})
        except requests.RequestException:
            _cooldown(COOLDOWN_FALHA, "rede")
            raise _Indisponivel(COOLDOWN_FALHA, "rede")

        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            try:
                ra = int(r.headers.get("Retry-After") or 0)
            except ValueError:
                ra = 0
            seg = max(ra, COOLDOWN_LIMITE)
            _cooldown(seg, "429")
            raise _Indisponivel(seg, "429")
        if r.status_code in (401, 403):
            _cooldown(COOLDOWN_LIMITE, str(r.status_code))
            raise _Indisponivel(COOLDOWN_LIMITE, str(r.status_code))
        if r.status_code >= 500:
            _cooldown(COOLDOWN_FALHA, str(r.status_code))
            raise _Indisponivel(COOLDOWN_FALHA, str(r.status_code))
        return None                                   # 404 etc.: sem resultado


def _consultar_bling(sku: str):
    """Retorna (nome, foto, completo). completo=False => não gravar no cache."""
    res = _bling_get("/produtos", {"codigo": sku, "limite": 5})
    data = (res or {}).get("data") or []
    if not data:
        return (None, None, True)
    prod = next((p for p in data if str(p.get("codigo", "")).lower() == sku.lower()), data[0])
    nome = prod.get("nome") or prod.get("descricao")
    foto = _foto_url(prod)
    if not foto and prod.get("id"):
        try:
            det = _bling_get(f"/produtos/{prod['id']}")
            foto = _foto_url((det or {}).get("data") or {})
        except _Indisponivel:
            return (nome, None, False)                # achou o nome, mas a foto fica pra depois
    return (nome, foto, True)


def _foto_url(prod: dict):
    """Primeira URL de imagem do produto do Bling."""
    if not isinstance(prod, dict):
        return None
    direto = (prod.get("imagemURL") or "").strip()
    if direto:
        return direto
    midia = prod.get("midia") or {}
    imgs = (midia.get("imagens") or {}) if isinstance(midia, dict) else {}
    for grupo in ("externas", "internas"):
        for im in (imgs.get(grupo) or []):
            if isinstance(im, str) and im.strip():
                return im.strip()
            if isinstance(im, dict):
                link = im.get("link") or im.get("linkMiniatura")
                if link:
                    return link
    for im in (imgs.get("imagensURL") or []):
        if isinstance(im, str) and im.strip():
            return im.strip()
        if isinstance(im, dict):
            link = im.get("link") or im.get("url") or im.get("linkMiniatura")
            if link:
                return link
    return None
