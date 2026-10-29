#!/usr/bin/env bash
# Instalador do Mapa do Galpao no backend do WMS (rode DENTRO da pasta do backend)
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi

# 1) cria o modulo mapa_routes.py
cat > mapa_routes.py <<'MAPAEOF'
"""
Mapa do Galpão — localização de SKU (backend, v2).

Endpoints (registrar com url_prefix="/api"):

    GET    /api/mapa/localizacoes        lista o que já foi mapeado (só banco, nunca chama o Bling)
    POST   /api/mapa/localizacao         adiciona {sku, porta_palet, posicao, nome, foto} (N:N)
    DELETE /api/mapa/localizacao/<sku>   remove o SKU (?porta_palet=&posicao= => só daquela posição)
    GET    /api/mapa/produto/<sku>       nome + foto (cache -> mapa -> Bling, nessa ordem)
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
import threading
import time

import requests
from flask import Blueprint, g, jsonify, request

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
VALID_PP = set(range(1, 11))          # Porta Palet 1..10
VALID_POS = set(range(1, 7))          # P1..P6

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
    if pp not in VALID_PP or pos not in VALID_POS:
        return jsonify({"error": "porta_palet 1-10 e posicao 1-6"}), 400

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


@mapa_routes.get("/mapa/status")
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
MAPAEOF

# 2) registra o blueprint no app.py (idempotente)
python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "mapa_routes" in s:
    print(">> app.py ja tinha o mapa_routes (ok, nada a fazer)")
else:
    linha_reg = '    from mapa_routes import mapa_routes as mapa_bp\n    app.register_blueprint(mapa_bp, url_prefix="/api")\n'
    if "app.register_blueprint(api_routes)" in s:
        s = s.replace("    app.register_blueprint(api_routes)\n",
                       "    app.register_blueprint(api_routes)\n" + linha_reg, 1)
    elif "app.register_blueprint(main_routes)" in s:
        s = s.replace("    app.register_blueprint(main_routes)\n",
                       "    app.register_blueprint(main_routes)\n" + linha_reg, 1)
    else:
        print("!! Nao achei onde registrar. Adicione manualmente dentro de create_app():")
        print(linha_reg); raise SystemExit(1)
    open(f, "w", encoding="utf-8").write(s)
    print(">> app.py patchado: blueprint /api/mapa/* registrado")
PYEOF

echo ""
echo "OK. Reinicie o backend (systemctl restart wms-russia / wms-europa) e teste."
