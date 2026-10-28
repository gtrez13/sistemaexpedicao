#!/usr/bin/env bash
# Instalador do Mapa do Galpao no backend do WMS (rode DENTRO da pasta do backend)
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi

# 1) cria o modulo mapa_routes.py
cat > mapa_routes.py <<'MAPAEOF'
"""
Mapa do Galpão — localização de SKU (backend).

Blueprint Flask com os 4 endpoints que a tela /mapa consome:

    GET    /api/mapa/localizacoes          -> lista tudo que já foi mapeado
    POST   /api/mapa/localizacao           -> grava/atualiza {sku,porta_palet,posicao,nome}
    DELETE /api/mapa/localizacao/<sku>      -> remove o SKU do mapa
    GET    /api/mapa/produto/<sku>          -> {sku,nome,foto} (foto vem do Bling)

INTEGRAÇÃO (2 pontos, marcados com # >>>):
  1. Conexão com o banco — aqui uso o db_session() do projeto antigo
     (sqlite). Se o backend de produção usa Postgres/outro, troque só as 2
     funções _db_* por uma query equivalente. A tabela é criada sozinha.
  2. Cliente Bling — uso services.bling_service.BlingService. Ajuste o import
     se o caminho for outro no backend de produção.

Registrar no app (onde os outros blueprints são registrados), com prefixo /api:
    from mapa_routes import mapa_routes
    app.register_blueprint(mapa_routes, url_prefix="/api")
"""
from __future__ import annotations
from flask import Blueprint, request, jsonify

# >>> 1. BANCO — ajuste este import pro seu backend de produção
from db.database import db_session

# >>> 2. BLING — ajuste este import pro seu backend de produção
try:
    from services.bling_service import BlingService
    _bling = BlingService()
except Exception:
    _bling = None

mapa_routes = Blueprint("mapa_routes", __name__)

VALID_PP = set(range(1, 11))   # Porta Palet 1..10
VALID_POS = set(range(1, 7))   # P1..P6


# ───────────────────────── tabela ─────────────────────────
def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS localizacao_sku (
            sku          TEXT PRIMARY KEY COLLATE NOCASE,
            nome         TEXT,
            porta_palet  INTEGER NOT NULL,
            posicao      INTEGER NOT NULL,
            atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # uma posição física guarda um SKU (opcional: remova se puder repetir)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_loc_posicao
        ON localizacao_sku(porta_palet, posicao)
    """)


# ───────────────────────── endpoints ─────────────────────────
@mapa_routes.get("/mapa/localizacoes")
def listar_localizacoes():
    with db_session() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT sku, nome, porta_palet, posicao FROM localizacao_sku"
        ).fetchall()
    itens = [
        {"sku": r["sku"], "nome": r["nome"],
         "porta_palet": r["porta_palet"], "posicao": r["posicao"],
         "foto": _foto_cache(r["sku"])}
        for r in rows
    ]
    return jsonify({"localizacoes": itens})


@mapa_routes.post("/mapa/localizacao")
def salvar_localizacao():
    d = request.get_json(silent=True) or {}
    sku = (d.get("sku") or "").strip()
    nome = (d.get("nome") or "").strip()
    try:
        pp = int(d.get("porta_palet"))
        pos = int(d.get("posicao"))
    except (TypeError, ValueError):
        return jsonify({"error": "porta_palet e posicao devem ser números"}), 400
    if not sku:
        return jsonify({"error": "sku é obrigatório"}), 400
    if pp not in VALID_PP or pos not in VALID_POS:
        return jsonify({"error": "porta_palet 1-10 e posicao 1-6"}), 400

    with db_session() as conn:
        _ensure_table(conn)
        # libera a posição se outro SKU estava nela (um SKU por posição)
        conn.execute(
            "DELETE FROM localizacao_sku WHERE porta_palet=? AND posicao=? AND sku<>?",
            (pp, pos, sku),
        )
        conn.execute("""
            INSERT INTO localizacao_sku (sku, nome, porta_palet, posicao, atualizado_em)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(sku) DO UPDATE SET
                nome=excluded.nome, porta_palet=excluded.porta_palet,
                posicao=excluded.posicao, atualizado_em=CURRENT_TIMESTAMP
        """, (sku, nome, pp, pos))
    return jsonify({"success": True, "sku": sku, "porta_palet": pp, "posicao": pos})


@mapa_routes.delete("/mapa/localizacao/<sku>")
def remover_localizacao(sku):
    with db_session() as conn:
        _ensure_table(conn)
        conn.execute("DELETE FROM localizacao_sku WHERE sku=?", (sku.strip(),))
    return jsonify({"success": True, "sku": sku})


@mapa_routes.get("/mapa/produto/<sku>")
def produto_bling(sku):
    sku = sku.strip()
    nome, foto = _buscar_produto_bling(sku)
    return jsonify({"sku": sku, "nome": nome, "foto": foto})


# ───────────────────────── Bling: nome + foto ─────────────────────────
_FOTO_CACHE: dict[str, str] = {}

def _foto_cache(sku: str) -> str | None:
    """Foto já conhecida (evita bater no Bling ao listar tudo)."""
    return _FOTO_CACHE.get((sku or "").lower())

def _buscar_produto_bling(sku: str):
    """Retorna (nome, foto_url) do produto pelo código, via Bling API v3."""
    if not _bling or not sku:
        return (None, None)
    try:
        # 1) acha o produto pelo código (SKU)
        res = _bling._get_json("/produtos", params={"codigo": sku})
        data = (res or {}).get("data") or []
        prod = data[0] if data else None
        if not prod:
            return (None, None)
        nome = prod.get("nome") or prod.get("descricao")
        pid = prod.get("id")
        # 2) imagem: resumo traz imagemURL de graça; senão abre o detalhe
        foto = (prod.get("imagemURL") or "").strip() or _extrair_foto(prod)
        if not foto and pid:
            det = _bling._get_json(f"/produtos/{pid}")
            foto = _extrair_foto((det or {}).get("data") or {})
        if foto:
            _FOTO_CACHE[sku.lower()] = foto
        return (nome, foto)
    except Exception:
        return (None, None)

def _extrair_foto(prod: dict) -> str | None:
    """Varre o produto do Bling atrás da 1ª URL de imagem (midia.imagens)."""
    if not isinstance(prod, dict):
        return None
    # campo direto (aparece no resumo da lista)
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
    # imagensURL: lista de strings (ou dicts) com as URLs
    for im in (imgs.get("imagensURL") or []):
        if isinstance(im, str) and im.strip():
            return im.strip()
        if isinstance(im, dict):
            link = im.get("link") or im.get("url") or im.get("linkMiniatura")
            if link:
                return link
    # fallback: qualquer campo que pareça URL de imagem
    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.startswith("http") and \
                   any(v.lower().endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
                    return v
                r = _walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = _walk(v)
                if r:
                    return r
        return None
    return _walk(prod)
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
