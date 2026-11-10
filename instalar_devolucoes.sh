#!/usr/bin/env bash
# Instalador do modulo DEVOLUCOES (rode DENTRO da pasta do backend, onde esta o app.py)
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

cat > devolucoes_routes.py <<'DEVEOF'
"""
Módulo de DEVOLUÇÕES (backend) — LHVMed.

A "caderneta" do recebimento: o operador abre a devolução, BIPA o produto (SKU) e confere item a item
(checklist), registra os dados que hoje vão no Google Forms e, no fim, o WMS entrega tudo ao SAC.

Endpoints (url_prefix="/api"; todos exigem login):
    GET   /api/devolucoes/opcoes             listas (lojas, motivos, chegada, destinos, status, itens do checklist)
    GET   /api/devolucoes/produto?q=         bip: acha o produto pelo SKU (devolve nome)
    GET   /api/devolucoes/venda?q=           acha o pedido pelo número da venda (cliente + itens)
    POST  /api/devolucoes                    cria a devolução (rascunho ou já completa)
    GET   /api/devolucoes[?status=&q=]       lista
    GET   /api/devolucoes/<id>               detalhe
    PATCH /api/devolucoes/<id>               edita campos / checklist / status
    POST  /api/devolucoes/importar-ml        traz do Mercado Livre as devoluções ABERTAS (status "A caminho")
    POST  /api/devolucoes/<id>/fotos         envia foto (multipart, campo "foto") de como o produto chegou
    GET   /api/devolucoes/<id>/fotos         lista as fotos
    GET   /api/devolucoes/fotos/<fid>        a imagem (exige login)    DELETE idem: remove
    POST  /api/devolucoes/<id>/resolver      supervisor/admin: {texto} resolve a pendência (problema na chegada / cobrança)
    POST  /api/devolucoes/<id>/finalizar     fecha a devolução (exige tudo preenchido, bipado, foto e pendência resolvida)
    POST  /api/devolucoes/<id>/sac           supervisor/admin: envia ao SAC (SAC_API_URL; sem isso fica "simulado")
    GET   /api/devolucoes/resumo             contadores do painel
    GET   /api/devolucoes/export.csv         supervisor/admin: planilha (mesmas colunas do painel)

NADA aqui escreve no Bling nem mexe em estoque: devolução só REGISTRA. Falha de integração com o SAC
nunca perde a devolução (fica "pendente"/"erro" e dá para reenviar).
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import uuid

from flask import Blueprint, Response, jsonify, request, send_file
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

devolucoes_routes = Blueprint("devolucoes_routes", __name__)
_tabelas_ok = False

LOJAS = ["LHV Med", "Medical Dantas", "FURIOLA", "MSDMED"]
MOTIVOS = ["Arrependimento ou desistência", "Produto diferente do anunciado", "Produto com defeito",
           "Compra não reconhecida", "Outro"]
CHEGADAS = ["Correto e sem danos aparentes", "Embalagem danificada, produto intacto", "Produto danificado",
            "Produto usado ou com sinais de uso", "Produto diferente do enviado", "Outro"]
DESTINOS = ["Estoque", "Assistência técnica", "Quarentena / análise", "Descarte", "Devolver ao fornecedor"]
STATUS = [("esperada", "A caminho (ML)"), ("armazem_ml", "No armazém do ML (FULL)"), ("recebida", "Recebida"), ("pendente_resolver", "Pendente: resolver"), ("aguardando_nf", "Aguardando NF de devolução"),
          ("assistencia", "Encaminhada à assistência"), ("liberada_estoque", "Liberada para estoque"),
          ("finalizada", "Finalizada")]
CHECKLIST = [
    ("lacre", "Lacre / embalagem original conferidos"),
    ("produto_confere", "Produto confere com o SKU da venda (bipado)"),
    ("acessorios", "Acessórios e manual conferidos"),
    ("sem_uso", "Sem sinais de uso"),
    ("sem_avaria", "Sem avaria visível"),
    ("serie_lote", "Número de série / lote anotado"),
    ("nf", "Nota fiscal de saída / devolução conferida"),
    ("fotos", "Fotos tiradas (se houver problema)"),
]


def _lista_env(nome, padrao):
    v = os.environ.get(nome, "").strip()
    return [x.strip() for x in v.split("|") if x.strip()] if v else padrao


def _role() -> str:
    return str(getattr(current_user, "role", "") or "").lower()


def _quem() -> str:
    return str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")[:80]


def eh_gestor() -> bool:
    return _role() in ("admin", "supervisor")


EXTRAS_V3 = [("resolucao", "TEXT"), ("resolvido_por", "TEXT"), ("resolvido_em", "BIGINT"), ("finalizada_em", "BIGINT"),
             ("finalizada_por", "TEXT"), ("dados_ml_em", "BIGINT"),
             ("ml_review", "TEXT"), ("ml_review_em", "BIGINT"), ("ml_review_por", "TEXT"), ("ml_review_msg", "TEXT")]
COLS_TXT = ["data_recebimento", "loja", "cliente", "numero_venda", "sku", "produto", "serie_lote", "nf_saida",
            "num_reclamacao", "motivo", "alegacao_defeito", "desc_avaria", "chegada", "qual_item", "destino",
            "observacoes", "status"]
COLS_BOOL = ["teve_reclamacao", "avaria", "faltou_item", "volta_estoque"]


def garantir_tabelas(conn=None):
    global _tabelas_ok
    if _tabelas_ok:
        return

    def _run(c):
        pk = "BIGSERIAL PRIMARY KEY" if os.environ.get("DATABASE_URL") else "INTEGER PRIMARY KEY AUTOINCREMENT"
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS devolucoes (
                id               {pk},
                criado_em        BIGINT NOT NULL,
                atualizado_em    BIGINT NOT NULL,
                responsavel      TEXT,
                data_recebimento TEXT,
                loja             TEXT,
                cliente          TEXT,
                numero_venda     TEXT,
                sku              TEXT,
                sku_key          TEXT,
                produto          TEXT,
                qtd              DOUBLE PRECISION NOT NULL DEFAULT 1,
                valor            DOUBLE PRECISION,
                custo_devolucao  DOUBLE PRECISION,
                custo_ml         DOUBLE PRECISION,
                custo_ml_em      BIGINT,
                ml_envio         TEXT,
                resolucao        TEXT,
                resolvido_por    TEXT,
                resolvido_em     BIGINT,
                finalizada_em    BIGINT,
                finalizada_por   TEXT,
                dados_ml_em      BIGINT,
                serie_lote       TEXT,
                nf_saida         TEXT,
                teve_reclamacao  INTEGER,
                num_reclamacao   TEXT,
                motivo           TEXT,
                alegacao_defeito TEXT,
                avaria           INTEGER,
                desc_avaria      TEXT,
                chegada          TEXT,
                faltou_item      INTEGER,
                qual_item        TEXT,
                volta_estoque    INTEGER,
                destino          TEXT,
                observacoes      TEXT,
                status           TEXT NOT NULL DEFAULT 'recebida',
                bipado           INTEGER NOT NULL DEFAULT 0,
                checklist        TEXT,
                sac_status       TEXT NOT NULL DEFAULT 'pendente',
                sac_msg          TEXT,
                sac_os_id        TEXT,
                sac_enviado_em   BIGINT
            )
        """)
        if os.environ.get("DATABASE_URL"):
            c.execute("ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS ml_claim_id TEXT")
            c.execute("ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS custo_devolucao DOUBLE PRECISION")
            c.execute("ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS custo_ml DOUBLE PRECISION")
            c.execute("ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS custo_ml_em BIGINT")
            c.execute("ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS ml_envio TEXT")
            for col, tp in EXTRAS_V3:
                c.execute(f"ALTER TABLE devolucoes ADD COLUMN IF NOT EXISTS {col} {tp}")
        else:
            have = {r[1] for r in c.execute("PRAGMA table_info(devolucoes)").fetchall()}
            if "ml_claim_id" not in have:
                c.execute("ALTER TABLE devolucoes ADD COLUMN ml_claim_id TEXT")
            if "custo_devolucao" not in have:
                c.execute("ALTER TABLE devolucoes ADD COLUMN custo_devolucao DOUBLE PRECISION")
            if "custo_ml" not in have:
                c.execute("ALTER TABLE devolucoes ADD COLUMN custo_ml DOUBLE PRECISION")
            if "custo_ml_em" not in have:
                c.execute("ALTER TABLE devolucoes ADD COLUMN custo_ml_em BIGINT")
            if "ml_envio" not in have:
                c.execute("ALTER TABLE devolucoes ADD COLUMN ml_envio TEXT")
            for col, tp in EXTRAS_V3:
                if col not in have:
                    c.execute(f"ALTER TABLE devolucoes ADD COLUMN {col} {tp}")
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS devolucao_fotos (
                id            {pk},
                devolucao_id  BIGINT NOT NULL,
                arquivo       TEXT NOT NULL,
                criado_em     BIGINT NOT NULL,
                criado_por    TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_devolucao_fotos ON devolucao_fotos (devolucao_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_devolucoes_status ON devolucoes (status, criado_em)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_devolucoes_venda ON devolucoes (numero_venda)")

    if conn is not None:
        _run(conn)
    else:
        with db_session() as c:
            _run(c)
    _tabelas_ok = True


def _bool(v):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        return 1 if v.strip().lower() in ("1", "true", "sim", "s", "yes") else 0
    return 1 if v else 0


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("R$", "").strip().replace(".", "").replace(",", ".")
                     if isinstance(v, str) and "," in v else v)
    except (TypeError, ValueError):
        return None


def _fmt(r):
    d = {k: r[k] for k in r.keys()}
    try:
        d["checklist"] = json.loads(d.get("checklist") or "{}")
    except ValueError:
        d["checklist"] = {}
    for k in COLS_BOOL:
        if d.get(k) is not None:
            d[k] = bool(d[k])
    d["bipado"] = bool(d.get("bipado"))
    d["tem_problema"] = tem_problema(r)
    d["pendencia"] = d["tem_problema"] and not d.get("resolvido_em")
    return d


def _fmt_com_fotos(conn, rows):
    ids = [r["id"] for r in rows]
    cont = {}
    for i in range(0, len(ids), 200):
        lote = ids[i:i + 200]
        for x in conn.execute(f"SELECT devolucao_id, COUNT(*) AS n FROM devolucao_fotos WHERE devolucao_id IN ({','.join('?' * len(lote))}) GROUP BY devolucao_id", tuple(lote)).fetchall():
            cont[x["devolucao_id"]] = x["n"]
    out = []
    for r in rows:
        d = _fmt(r)
        d["n_fotos"] = cont.get(r["id"], 0)
        out.append(d)
    return out


# ───────────────────────────── regras: problema na chegada, pendência, fechamento ─────────────────────────────
def tem_problema(d) -> bool:
    """Chegou com problema: avaria, faltou item, ou 'de que maneira chegou' diferente de correto/sem danos."""
    ch = (d["chegada"] if d["chegada"] is not None else "") or ""
    return bool(d["avaria"]) or bool(d["faltou_item"]) or (ch != "" and ch != CHEGADAS[0])


def _recalcular(conn, did):
    """Problema na chegada sem resolução => 'pendente_resolver'. Resolvido (ou sem problema) => volta a 'recebida'."""
    r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r or r["status"] not in ("recebida", "pendente_resolver"):
        return
    if tem_problema(r) and not r["resolvido_em"]:
        if r["status"] != "pendente_resolver":
            conn.execute("UPDATE devolucoes SET status='pendente_resolver' WHERE id=?", (did,))
    elif r["status"] == "pendente_resolver":
        conn.execute("UPDATE devolucoes SET status='recebida' WHERE id=?", (did,))


def _n_fotos(conn, did) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM devolucao_fotos WHERE devolucao_id=?", (did,)).fetchone()["n"]


def faltando_para_fechar(r, n_fotos) -> list:
    f = []
    for campo, nome in (("sku", "SKU"), ("loja", "loja"), ("cliente", "cliente"), ("numero_venda", "número da venda"),
                        ("nf_saida", "NF de saída"), ("motivo", "motivo"), ("chegada", "como o produto chegou"),
                        ("destino", "destino")):
        if not (r[campo] or "").strip():
            f.append(nome)
    if r["valor"] is None:
        f.append("valor")
    for campo, nome in (("teve_reclamacao", "reclamação (sim/não)"), ("avaria", "avaria (sim/não)"),
                        ("faltou_item", "faltou item (sim/não)"), ("volta_estoque", "pode voltar ao estoque")):
        if r[campo] is None:
            f.append(nome)
    if not r["bipado"]:
        f.append("bipar o produto")
    if n_fotos < 1:
        f.append("FOTO de como o produto chegou")
    if tem_problema(r) and not r["resolvido_em"]:
        f.append("resolver a pendência (problema na chegada)")
    return f


# ───────────────────────────── consultas de apoio ─────────────────────────────
@devolucoes_routes.get("/devolucoes/opcoes")
@login_required
def opcoes():
    return jsonify({
        "lojas": _lista_env("DEVOLUCAO_LOJAS", LOJAS),
        "motivos": _lista_env("DEVOLUCAO_MOTIVOS", MOTIVOS),
        "chegadas": CHEGADAS,
        "destinos": _lista_env("DEVOLUCAO_DESTINOS", DESTINOS),
        "status": [{"v": v, "label": l} for v, l in STATUS],
        "checklist": [{"v": v, "label": l} for v, l in CHECKLIST],
        "eh_gestor": eh_gestor(),
        "usuario": _quem(),
    })


@devolucoes_routes.get("/devolucoes/produto")
@login_required
def produto():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "informe o SKU"}), 400
    with db_session() as conn:
        r = conn.execute("SELECT sku, nome FROM produtos WHERE LOWER(sku)=LOWER(?) LIMIT 1", (q,)).fetchone()
    if not r:
        return jsonify({"encontrado": False, "sku": q})
    return jsonify({"encontrado": True, "sku": r["sku"], "nome": r["nome"]})


@devolucoes_routes.get("/devolucoes/venda")
@login_required
def venda():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "informe o número da venda"}), 400
    garantir_tabelas()
    with db_session() as conn:
        ex = conn.execute("SELECT id, status FROM devolucoes WHERE numero_venda=? ORDER BY id DESC LIMIT 1", (q,)).fetchone()
        existente = {"id": ex["id"], "status": ex["status"]} if ex else None
        p = conn.execute("SELECT id, ml_id, cliente_nome FROM pedidos WHERE ml_id=? LIMIT 1", (q,)).fetchone()
        if not p:
            return jsonify({"encontrado": False, "devolucao_existente": existente})
        itens = conn.execute(
            "SELECT pr.sku AS sku, pr.nome AS nome, pp.quantidade AS qtd FROM pedido_produtos pp "
            "JOIN produtos pr ON pr.id = pp.produto_id WHERE pp.pedido_id=?", (p["id"],)).fetchall()
    return jsonify({"encontrado": True, "numero_venda": p["ml_id"], "cliente": p["cliente_nome"],
                    "devolucao_existente": existente,
                    "itens": [{"sku": i["sku"], "nome": i["nome"], "qtd": i["qtd"]} for i in itens]})


# ───────────────────────────── CRUD ─────────────────────────────
def _aplicar(d: dict, base: dict | None = None) -> dict:
    """Normaliza o corpo recebido -> dict de colunas."""
    out = dict(base or {})
    for k in COLS_TXT:
        if k in d:
            out[k] = (str(d[k]).strip()[:500] if d[k] not in (None, "") else None)
    for k in COLS_BOOL:
        if k in d:
            out[k] = _bool(d[k])
    if "qtd" in d:
        try:
            out["qtd"] = max(float(d["qtd"]), 0.0) or 1.0
        except (TypeError, ValueError):
            out["qtd"] = 1.0
    if "valor" in d:
        out["valor"] = _num(d["valor"])
    if "custo_devolucao" in d:
        out["custo_devolucao"] = _num(d["custo_devolucao"])
    if "bipado" in d:
        out["bipado"] = 1 if d["bipado"] else 0
    if "checklist" in d and isinstance(d["checklist"], dict):
        validos = {v for v, _ in CHECKLIST}
        out["checklist"] = json.dumps({k: bool(x) for k, x in d["checklist"].items() if k in validos})
    if out.get("sku"):
        out["sku_key"] = out["sku"][:64].lower()
    if out.get("status") and out["status"] not in {v for v, _ in STATUS}:
        out["status"] = "recebida"
    return out


@devolucoes_routes.post("/devolucoes")
@login_required
def criar():
    d = request.get_json(silent=True) or {}
    if not (d.get("sku") or "").strip():
        return jsonify({"error": "Bipe ou digite o SKU do produto."}), 400
    garantir_tabelas()
    agora = int(time.time())
    base = {"criado_em": agora, "atualizado_em": agora, "responsavel": _quem(), "qtd": 1.0,
            "status": "recebida", "bipado": 0, "checklist": "{}"}
    dados = _aplicar(d, base)
    dados.setdefault("data_recebimento", time.strftime("%Y-%m-%d"))
    if not dados.get("data_recebimento"):
        dados["data_recebimento"] = time.strftime("%Y-%m-%d")
    cols = list(dados.keys())
    with db_session() as conn:
        # toque duplo: mesma venda+SKU nos últimos 2 min -> devolve a existente
        if dados.get("numero_venda"):
            ex = conn.execute("SELECT id FROM devolucoes WHERE numero_venda=? AND sku_key=? AND criado_em > ? "
                              "ORDER BY id DESC LIMIT 1", (dados["numero_venda"], dados["sku_key"], agora - 120)).fetchone()
            if ex:
                return jsonify({"success": True, "id": ex["id"], "duplicado": True})
        conn.execute(f"INSERT INTO devolucoes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     tuple(dados[c] for c in cols))
        nid = conn.execute("SELECT id FROM devolucoes WHERE criado_em=? AND responsavel=? ORDER BY id DESC LIMIT 1",
                           (agora, dados["responsavel"])).fetchone()["id"]
        _recalcular(conn, nid)
    return jsonify({"success": True, "id": nid})


@devolucoes_routes.get("/devolucoes")
@login_required
def listar():
    garantir_tabelas()
    st = (request.args.get("status") or "").strip()
    q = (request.args.get("q") or "").strip().lower()
    sql, args, w = "SELECT * FROM devolucoes", [], []
    if st and st != "todas":
        w.append("status=?"); args.append(st)
    if q:
        w.append("(LOWER(COALESCE(numero_venda,'')) LIKE ? OR LOWER(COALESCE(sku,'')) LIKE ? OR LOWER(COALESCE(cliente,'')) LIKE ?)")
        args += [f"%{q}%"] * 3
    if w:
        sql += " WHERE " + " AND ".join(w)
    sql += " ORDER BY criado_em DESC LIMIT 300"
    with db_session() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
        itens = _fmt_com_fotos(conn, rows)
    return jsonify({"itens": itens})


@devolucoes_routes.get("/devolucoes/resumo")
@login_required
def resumo():
    garantir_tabelas()
    with db_session() as conn:
        tot = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(valor*qtd),0) AS v, COALESCE(SUM(COALESCE(custo_devolucao, custo_ml, 0)),0) AS c FROM devolucoes").fetchone()
        volt = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(valor*qtd),0) AS v FROM devolucoes WHERE volta_estoque=1").fetchone()
        por = conn.execute("SELECT status, COUNT(*) AS n FROM devolucoes GROUP BY status").fetchall()
        sac = conn.execute("SELECT COUNT(*) AS n FROM devolucoes WHERE sac_status<>'enviado'").fetchone()["n"]
    por_status_map = {r["status"]: r["n"] for r in por}
    return jsonify({"total": tot["n"], "valor": float(tot["v"] or 0),
                    "valor_voltou": float(volt["v"] or 0),          # produtos que voltaram ao estoque (verde)
                    "custo_devolucao": float(tot["c"] or 0),        # o que a empresa arcou para a devolução (vermelho)
                    "por_status": {r["status"]: r["n"] for r in por},
                    "sac_pendentes": sac, "voltam_estoque": volt["n"],
                    "pendencias": dict(por_status_map).get("pendente_resolver", 0)})


@devolucoes_routes.get("/devolucoes/<int:did>")
@login_required
def detalhe(did):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        d = _fmt_com_fotos(conn, [r])[0]
        d["fotos"] = [x["id"] for x in conn.execute("SELECT id FROM devolucao_fotos WHERE devolucao_id=? ORDER BY id", (did,)).fetchall()]
    return jsonify(d)


@devolucoes_routes.patch("/devolucoes/<int:did>")
@login_required
def editar(did):
    d = request.get_json(silent=True) or {}
    garantir_tabelas()
    novos = _aplicar(d)
    novos.pop("sku_key", None) if "sku" not in d else None
    if novos.get("status") in ("finalizada", "pendente_resolver"):   # só pelos endpoints /finalizar e /resolver (ou automático)
        novos.pop("status")
    if not novos:
        return jsonify({"error": "Nada para alterar."}), 400
    novos["atualizado_em"] = int(time.time())
    sets = ",".join(f"{c}=?" for c in novos)
    with db_session() as conn:
        r = conn.execute("SELECT id, sac_status, status FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        if r["status"] == "finalizada" and not eh_gestor():
            return jsonify({"error": "Devolução finalizada: só supervisor/admin altera."}), 403
        # alterou depois de enviada ao SAC: volta a "pendente" para reenviar a versão certa
        if r["sac_status"] == "enviado":
            sets += ",sac_status='pendente'"
        conn.execute(f"UPDATE devolucoes SET {sets} WHERE id=?", tuple(novos.values()) + (did,))
        _recalcular(conn, did)
        st = conn.execute("SELECT status FROM devolucoes WHERE id=?", (did,)).fetchone()["status"]
    return jsonify({"success": True, "status": st})


# ───────────────────────────── fotos ─────────────────────────────
MAX_FOTO = 8 * 1024 * 1024


def _pasta_fotos():
    d = os.environ.get("DEVOLUCOES_FOTOS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads_devolucoes")
    os.makedirs(d, exist_ok=True)
    return d


def _tipo_imagem(b: bytes):
    if b[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None, None


@devolucoes_routes.post("/devolucoes/<int:did>/fotos")
@login_required
def foto_enviar(did):
    f = request.files.get("foto")
    if not f:
        return jsonify({"error": "Envie a foto (campo 'foto')."}), 400
    dados = f.read(MAX_FOTO + 1)
    if len(dados) > MAX_FOTO:
        return jsonify({"error": "Foto grande demais (máx. 8 MB)."}), 413
    ext, _ = _tipo_imagem(dados)
    if not ext:
        return jsonify({"error": "Arquivo não é uma imagem (JPG, PNG ou WEBP)."}), 400
    garantir_tabelas()
    with db_session() as conn:
        if not conn.execute("SELECT 1 FROM devolucoes WHERE id=?", (did,)).fetchone():
            return jsonify({"error": "Devolução não encontrada."}), 404
        nome = f"{did}_{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(_pasta_fotos(), nome), "wb") as out:
            out.write(dados)
        conn.execute("INSERT INTO devolucao_fotos (devolucao_id, arquivo, criado_em, criado_por) VALUES (?,?,?,?)",
                     (did, nome, int(time.time()), _quem()))
        fid = conn.execute("SELECT id FROM devolucao_fotos WHERE arquivo=?", (nome,)).fetchone()["id"]
    return jsonify({"success": True, "id": fid})


@devolucoes_routes.get("/devolucoes/<int:did>/fotos")
@login_required
def foto_listar(did):
    garantir_tabelas()
    with db_session() as conn:
        rows = conn.execute("SELECT id, criado_por, criado_em FROM devolucao_fotos WHERE devolucao_id=? ORDER BY id", (did,)).fetchall()
    return jsonify({"fotos": [{"id": r["id"], "por": r["criado_por"], "em": r["criado_em"]} for r in rows]})


@devolucoes_routes.get("/devolucoes/fotos/<int:fid>")
@login_required
def foto_ver(fid):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT arquivo FROM devolucao_fotos WHERE id=?", (fid,)).fetchone()
    if not r:
        return jsonify({"error": "Foto não encontrada."}), 404
    caminho = os.path.join(_pasta_fotos(), os.path.basename(r["arquivo"]))
    if not os.path.exists(caminho):
        return jsonify({"error": "Arquivo da foto não encontrado."}), 404
    _, mime = _tipo_imagem(open(caminho, "rb").read(16))
    return send_file(caminho, mimetype=mime or "application/octet-stream", max_age=86400)


@devolucoes_routes.delete("/devolucoes/fotos/<int:fid>")
@login_required
def foto_apagar(fid):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT arquivo, criado_por, devolucao_id FROM devolucao_fotos WHERE id=?", (fid,)).fetchone()
        if not r:
            return jsonify({"error": "Foto não encontrada."}), 404
        if not eh_gestor() and r["criado_por"] != _quem():
            return jsonify({"error": "Só quem enviou ou o supervisor remove a foto."}), 403
        conn.execute("DELETE FROM devolucao_fotos WHERE id=?", (fid,))
    try:
        os.remove(os.path.join(_pasta_fotos(), os.path.basename(r["arquivo"])))
    except OSError:
        pass
    return jsonify({"success": True})


# ───────────────────────────── resolver pendência / finalizar ─────────────────────────────
@devolucoes_routes.post("/devolucoes/<int:did>/resolver")
@login_required
def resolver(did):
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin resolve a pendência."}), 403
    texto = ((request.get_json(silent=True) or {}).get("texto") or "").strip()
    if len(texto) < 10:
        return jsonify({"error": "Explique como foi resolvido (mín. 10 caracteres)."}), 400
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT status FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        conn.execute("UPDATE devolucoes SET resolucao=?, resolvido_por=?, resolvido_em=?, atualizado_em=? WHERE id=?",
                     (texto[:1000], _quem(), int(time.time()), int(time.time()), did))
        _recalcular(conn, did)
        st = conn.execute("SELECT status FROM devolucoes WHERE id=?", (did,)).fetchone()["status"]
    return jsonify({"success": True, "status": st})


@devolucoes_routes.post("/devolucoes/<int:did>/finalizar")
@login_required
def finalizar(did):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        if r["status"] == "finalizada":
            return jsonify({"success": True, "ja_finalizada": True})
        falta = faltando_para_fechar(r, _n_fotos(conn, did))
        if falta:
            return jsonify({"error": "Ainda falta: " + ", ".join(falta) + ".", "falta": falta}), 409
        agora = int(time.time())
        conn.execute("UPDATE devolucoes SET status='finalizada', finalizada_em=?, finalizada_por=?, atualizado_em=? WHERE id=?",
                     (agora, _quem(), agora, did))
    return jsonify({"success": True})


# ───────────────────────────── REVISÃO DA DEVOLUÇÃO NO MERCADO LIVRE ─────────────────────────────
# Depois de conferir, o operador avisa o ML: "chegou tudo certo" (return-review-ok) ou "chegou com problema"
# (return-review-fail, com motivo SRFx, mensagem e fotos). Doc: developers.mercadolibre.com.ar/devsite/manage-returns
ML_API = "https://api.mercadolibre.com"
MOTIVOS_FAIL_PADRAO = [("SRF2", "O produto chegou danificado"), ("SRF3", "A devolução está incompleta"),
                       ("SRF4", "O produto devolvido é diferente do enviado"), ("SRF5", "O produto não está no pacote"),
                       ("SRF6", "Outro defeito no produto"), ("SRF7", "A devolução ainda não chegou")]


def _ml_req(method, path, **kw):
    """Chamada ao ML com o token do WMS (renova 1x se vier 401). -> (status, json|texto)."""
    import requests as _rq
    from services.ml_service import MercadoLivreService
    ml = MercadoLivreService()
    for tent in (1, 2):
        r = _rq.request(method, ML_API + path, headers={"Authorization": f"Bearer {ml.access_token}"}, timeout=30, **kw)
        if r.status_code == 401 and tent == 1:
            try:
                ml.renovar_token()
            except Exception:
                pass
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    return 401, "token do ML inválido"


def _acoes_seller(claim: dict) -> list:
    for p in (claim or {}).get("players") or []:
        if p.get("type") == "seller" or p.get("role") == "respondent":
            return [a.get("action") for a in (p.get("available_actions") or []) if a.get("action")]
    return []


@devolucoes_routes.get("/devolucoes/<int:did>/ml-revisao")
@login_required
def ml_revisao_info(did):
    """Pode revisar no ML agora? + motivos de 'problema'."""
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT ml_claim_id, ml_review, ml_review_em, ml_review_por, ml_review_msg FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r:
        return jsonify({"error": "Devolução não encontrada."}), 404
    out = {"claim": r["ml_claim_id"], "feita": r["ml_review"], "feita_em": r["ml_review_em"], "feita_por": r["ml_review_por"],
           "msg": r["ml_review_msg"], "pode_ok": False, "pode_fail": False, "motivos": [{"id": a, "nome": b} for a, b in MOTIVOS_FAIL_PADRAO]}
    if not r["ml_claim_id"]:
        out["aviso"] = "Esta devolução não veio do Mercado Livre."
        return jsonify(out)
    st, claim = _ml_req("GET", f"/marketplace/v2/claims/{r['ml_claim_id']}")
    if st != 200:
        st, claim = _ml_req("GET", f"/post-purchase/v1/claims/{r['ml_claim_id']}")
    if st != 200:
        out["aviso"] = f"ML respondeu {st} ao consultar a reclamação: {str(claim)[:300]}"
        return jsonify(out)
    acoes = _acoes_seller(claim)
    out.update({"pode_ok": "return_review_ok" in acoes, "pode_fail": "return_review_fail" in acoes,
                "acoes": acoes, "status_claim": claim.get("status"), "stage": claim.get("stage")})
    st2, mot = _ml_req("GET", "/marketplace/v2/returns/reasons/return-fail")
    if st2 == 200 and isinstance(mot, list) and mot:
        out["motivos"] = [{"id": m.get("id"), "nome": m.get("detail") or m.get("name") or m.get("id")} for m in mot if m.get("id")]
    if not (out["pode_ok"] or out["pode_fail"]) and not out["feita"]:
        out["aviso"] = ("O ML ainda não liberou a revisão desta devolução (ações disponíveis: "
                        + (", ".join(acoes) or "nenhuma") + f"; reclamação {claim.get('status')}/{claim.get('stage')}).")
    return jsonify(out)


@devolucoes_routes.post("/devolucoes/<int:did>/ml-revisao")
@login_required
def ml_revisao_enviar(did):
    """body: {"ok": true}  ou  {"ok": false, "motivo": "SRF2", "mensagem": "...", "fotos": [ids]}"""
    garantir_tabelas()
    b = request.get_json(silent=True) or {}
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        fotos = conn.execute("SELECT id, arquivo FROM devolucao_fotos WHERE devolucao_id=? ORDER BY id", (did,)).fetchall()
    claim = r["ml_claim_id"]
    if not claim:
        return jsonify({"error": "Esta devolução não veio do Mercado Livre (sem número de reclamação)."}), 400
    if r["ml_review"]:
        return jsonify({"error": f"O ML já foi avisado ({r['ml_review']}) por {r['ml_review_por']}."}), 409
    if b.get("ok"):
        st, resp = _ml_req("POST", f"/marketplace/v2/claims/{claim}/actions/return-review-ok")
        resultado, msg = "ok", "Chegou tudo certo"
    else:
        motivo = (b.get("motivo") or "").strip()
        mensagem = (b.get("mensagem") or "").strip()
        if not motivo or not mensagem:
            return jsonify({"error": "Escolha o motivo e escreva a mensagem para o ML."}), 400
        quer = {int(x) for x in (b.get("fotos") or []) if str(x).isdigit()} or {f["id"] for f in fotos}
        anexos, erros = [], []
        for f in fotos:
            if f["id"] not in quer:
                continue
            caminho = os.path.join(_pasta_fotos(), os.path.basename(f["arquivo"]))
            if not os.path.exists(caminho):
                continue
            ext, mime = _tipo_imagem(open(caminho, "rb").read(16))
            with open(caminho, "rb") as fh:
                sa, ra = _ml_req("POST", f"/post-purchase/v1/claims/{claim}/returns/attachments",
                                 files={"file": (f"foto_{f['id']}.{ext or 'jpg'}", fh, mime or "image/jpeg")})
            if sa in (200, 201) and isinstance(ra, dict) and ra.get("file_name"):
                anexos.append(ra["file_name"])
            else:
                erros.append(f"foto {f['id']}: ML {sa} {str(ra)[:200]}")
        if motivo in ("SRF2", "SRF4") and not anexos:
            return jsonify({"error": "O ML exige foto para este motivo e nenhuma foto subiu. " + " | ".join(erros)}), 400
        st, resp = _ml_req("POST", f"/marketplace/v2/claims/{claim}/actions/return-review-fail",
                           json={"reason": motivo, "message": mensagem, "attachments": anexos})
        resultado, msg = "problema", f"{motivo}: {mensagem}" + (f" ({len(anexos)} foto(s))" if anexos else "")
    if st not in (200, 201):
        return jsonify({"error": f"O ML recusou ({st}): {str(resp)[:500]}"}), 502
    agora = int(time.time())
    with db_session() as conn:
        conn.execute("UPDATE devolucoes SET ml_review=?, ml_review_em=?, ml_review_por=?, ml_review_msg=?, atualizado_em=? WHERE id=?",
                     (resultado, agora, _quem(), msg, agora, did))
    return jsonify({"success": True, "resultado": resultado, "msg": msg})


# ───────────────────────────── DEVOLUÇÃO FULL: voltou mesmo pro estoque do ML? ─────────────────────────────
# Histórico do estoque FULL (operações 'sale_return' = devolução do comprador). Doc: devsite/fulfillment-stock
STATUS_FULL = {"damaged": "chegou danificado", "damaged_in_full": "danificado no armazém", "return_problem": "devolvido com problema",
               "lost": "perdido", "not_supported": "não aceito pelo FULL", "not supportted": "não aceito pelo FULL",
               "transfer": "em transferência", "withdrawal": "em retirada", "internal_process": "processo interno do ML"}


def _inventory_id(numero_venda, sku):
    st, o = _ml_req("GET", f"/orders/{numero_venda}")
    if st != 200 or not isinstance(o, dict):
        return None, None
    alvo = None
    for oi in o.get("order_items") or []:
        it = oi.get("item") or {}
        if not alvo or (sku and str(it.get("seller_sku") or "").upper() == str(sku).upper()):
            alvo = it
    if not alvo:
        return None, None
    st, item = _ml_req("GET", f"/items/{alvo.get('id')}")
    if st != 200 or not isinstance(item, dict):
        return None, alvo.get("id")
    var = alvo.get("variation_id")
    for v in item.get("variations") or []:
        if var and str(v.get("id")) == str(var) and v.get("inventory_id"):
            return v["inventory_id"], alvo.get("id")
    return item.get("inventory_id"), alvo.get("id")


@devolucoes_routes.get("/devolucoes/<int:did>/full")
@login_required
def full_retorno(did):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT numero_venda, sku, criado_em, qtd FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r or not r["numero_venda"]:
        return jsonify({"error": "Devolução sem número de venda."}), 404
    inv, item_id = _inventory_id(r["numero_venda"], r["sku"])
    if not inv:
        return jsonify({"ok": True, "full": False, "msg": "Este anúncio não tem estoque FULL (inventory_id não encontrado)." , "item_id": item_id})
    from datetime import datetime, timedelta, timezone
    ini = datetime.fromtimestamp((r["criado_em"] or time.time()) - 86400, tz=timezone.utc)
    fim = datetime.now(timezone.utc)
    if (fim - ini).days > 59:
        ini = fim - timedelta(days=59)
    seller = str(os.environ.get("ML_SELLER_ID", "")).strip()
    ops, ultimo_erro = [], None
    for base in ("/marketplace/stock/fulfillment/operations/search", "/stock/fulfillment/operations/search"):
        for tipo in ("sale_return", "sale_delivery_cancelation"):
            st, j = _ml_req("GET", base, params={"seller_id": seller, "inventory_id": inv, "type": tipo, "limit": 100,
                                                 "date_from": ini.strftime("%Y-%m-%d"), "date_to": fim.strftime("%Y-%m-%d")})
            if st == 200 and isinstance(j, dict):
                ops += j.get("results") or []
            else:
                ultimo_erro = f"ML {st}: {str(j)[:200]}"
        if ops:
            break
    saida = []
    for op in sorted(ops, key=lambda x: x.get("date_created") or ""):
        det = op.get("detail") or {}
        res = op.get("result") or {}
        bloq = [{"status": d.get("status"), "nome": STATUS_FULL.get(d.get("status"), d.get("status")), "qtd": d.get("quantity")}
                for d in (det.get("not_available_detail") or []) if d.get("quantity")]
        saida.append({"data": op.get("date_created"), "tipo": op.get("type"),
                      "voltou_disponivel": det.get("available_quantity") or 0, "voltou_bloqueado": bloq,
                      "estoque_full_depois": res.get("available_quantity"), "bloqueado_full_depois": res.get("not_available_quantity")})
    out = {"ok": True, "full": True, "inventory_id": inv, "item_id": item_id, "operacoes": saida}
    if saida:
        u = saida[-1]
        out["situacao"] = "disponivel" if u["voltou_disponivel"] and not u["voltou_bloqueado"] else ("bloqueado" if u["voltou_bloqueado"] else "registrado")
    else:
        out["situacao"] = "nao_chegou"
        if ultimo_erro:
            out["aviso"] = ultimo_erro
    return jsonify(out)


# ───────────────────────────── SAC ─────────────────────────────
def _payload_sac(r) -> dict:
    """Mapeia a devolução para os campos-ponte da O.S. do SAC (+ o detalhe completo em 'devolucao')."""
    f = _fmt(r)
    problema = "; ".join(x for x in [
        f"Motivo: {f['motivo']}" if f.get("motivo") else "",
        f"Alegação: {f['alegacao_defeito']}" if f.get("alegacao_defeito") else "",
        f"Chegada: {f['chegada']}" if f.get("chegada") else "",
        f"Avaria: {f['desc_avaria']}" if f.get("avaria") and f.get("desc_avaria") else "",
        f"Faltou: {f['qual_item']}" if f.get("faltou_item") and f.get("qual_item") else "",
        f"Destino: {f['destino']}" if f.get("destino") else ""] if x)
    return {
        "origem": "wms-lhvmed", "tipo": "devolucao", "wms_id": f["id"],
        "name": f.get("cliente"), "numero_nf": f.get("nf_saida"), "invoice_number": f.get("nf_saida"),
        "product": f"{f.get('sku')} - {f.get('produto') or ''}".strip(" -"),
        "problem_type": f.get("motivo"), "problem": problema, "empresa": f.get("loja"),
        "criado_por": f.get("responsavel"), "devolucao": f,
    }


def enviar_ao_sac(r):
    """-> (status, msg, os_id). status: enviado | simulado | erro."""
    base = os.environ.get("SAC_API_URL", "").strip().rstrip("/")
    if not base:
        return "simulado", "SAC_API_URL não definido: nada foi enviado (modo simulado).", None
    path = os.environ.get("SAC_DEVOLUCAO_PATH", "/devolucoes").strip()
    try:
        import requests
        h = {"Content-Type": "application/json"}
        tok = os.environ.get("SAC_API_TOKEN", "").strip()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        resp = requests.post(base + path, json=_payload_sac(r), headers=h, timeout=15)
        if resp.status_code >= 300:
            return "erro", f"SAC respondeu HTTP {resp.status_code}: {resp.text[:200]}", None
        try:
            j = resp.json()
        except ValueError:
            j = {}
        return "enviado", "ok", str(j.get("id") or j.get("os_id") or "") or None
    except Exception as e:  # noqa: BLE001
        return "erro", f"falha ao falar com o SAC: {e}"[:300], None


@devolucoes_routes.post("/devolucoes/<int:did>/sac")
@login_required
def sac(did):
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin envia ao SAC."}), 403
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        st, msg, osid = enviar_ao_sac(r)
        conn.execute("UPDATE devolucoes SET sac_status=?, sac_msg=?, sac_os_id=?, sac_enviado_em=? WHERE id=?",
                     ("enviado" if st == "enviado" else ("pendente" if st == "simulado" else "erro"),
                      msg, osid, int(time.time()) if st == "enviado" else None, did))
    return jsonify({"success": st != "erro", "sac_status": st, "msg": msg, "os_id": osid})


@devolucoes_routes.get("/devolucoes/export.csv")
@login_required
def exportar():
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin."}), 403
    garantir_tabelas()
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM devolucoes ORDER BY criado_em DESC").fetchall()
    cols = ["id", "data_recebimento", "responsavel", "loja", "cliente", "numero_venda", "sku", "produto", "qtd",
            "valor", "custo_devolucao", "serie_lote", "nf_saida", "teve_reclamacao", "num_reclamacao", "motivo", "alegacao_defeito",
            "avaria", "desc_avaria", "chegada", "faltou_item", "qual_item", "volta_estoque", "destino", "status",
            "sac_status"]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(cols)
    for r in rows:
        f = _fmt(r)
        w.writerow([("sim" if f[c] is True else "não" if f[c] is False else ("" if f[c] is None else f[c])) for c in cols])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=devolucoes.csv"})


# ───────────────────────────── importar do Mercado Livre ─────────────────────────────
_ultima_importacao = 0.0
ORCAMENTO_POR_RODADA = 25          # reclamações checadas por rodada (o ML limita as consultas: 429)
RECHECAR_SEM_DEVOLUCAO_S = 1800    # reclamação sem devolução ainda: olha de novo depois de 30 min
_reasons_cache: dict = {}


def _ml_get(path, params=None):
    """GET no ML com o token do WMS (o serviço renova sozinho). -> (http_status, json|None)."""
    from services.ml_service import MercadoLivreService
    r = MercadoLivreService()._request("GET", path, params=params)
    if r is None:
        return 0, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def _dados_do_pedido(order: dict) -> dict:
    itens = order.get("order_items") or []
    it = (itens[0] if itens else {}) or {}
    item = it.get("item") or {}
    b = order.get("buyer") or {}
    nome = " ".join(x for x in [b.get("first_name"), b.get("last_name")] if x) or b.get("nickname") or ""
    return {
        "cliente": nome[:200],
        "sku": str(item.get("seller_sku") or item.get("seller_custom_field") or "")[:64] or None,
        "produto": (item.get("title") or "")[:200],
        "qtd": it.get("quantity") or 1,
        "valor": it.get("unit_price"),
    }


def _destino_da_devolucao(ret: dict):
    """-> (tem_envio_de_volta, destino, status_envio). destino: 'warehouse' (armazém do ML) | 'seller' | outro."""
    for sh in (ret.get("shipments") or []):
        if (sh.get("type") or "return") == "return":
            return True, ((sh.get("destination") or {}).get("name") or "").lower(), sh.get("status")
    return False, "", None


def _motivo_texto(ml_get, reason_id):
    """Texto do motivo no ML (cacheado); se não conseguir, devolve só o código."""
    if not reason_id:
        return "—"
    if reason_id not in _reasons_cache:
        code, j = ml_get(f"/post-purchase/v1/claims/reasons/{reason_id}")
        txt = ((j or {}).get("detail") or (j or {}).get("name")) if code == 200 else None
        _reasons_cache[reason_id] = f"{txt} ({reason_id})" if txt else reason_id
    return _reasons_cache[reason_id]


ORCAMENTO_CUSTOS = 15             # devoluções cujo frete de volta é consultado por rodada (2 chamadas cada)
RECHECAR_CUSTO_S = 2 * 3600       # custo e status do envio de devolução ainda aberta: atualiza a cada 2 h


class _LimiteML(Exception):
    pass


def _custo_frete_volta(ml_get, claim_id):
    """Frete de volta cobrado do VENDEDOR (receiver.cost) + status do envio de volta. -> (valor|None, leu_algum_envio, status_envio)."""
    code, ret = ml_get(f"/post-purchase/v2/claims/{claim_id}/returns")
    if code == 429:
        raise _LimiteML()
    if code != 200 or not ret:
        return None, False, None
    seller = str(os.environ.get("ML_SELLER_ID", "")).strip()
    total, leu, st_envio = 0.0, False, None
    for sh in (ret.get("shipments") or []):
        if (sh.get("type") or "return") != "return":
            continue
        st_envio = st_envio or sh.get("status")
        if not sh.get("shipment_id"):
            continue
        code2, cs = ml_get(f"/shipments/{sh['shipment_id']}/costs")
        if code2 == 429:
            raise _LimiteML()
        if code2 != 200 or not cs:
            continue
        leu = True
        rc = cs.get("receiver") or {}
        if not seller or str(rc.get("user_id")) == seller:
            try:
                total += float(rc.get("cost") or 0)
            except (TypeError, ValueError):
                pass
    return (total if leu else None), leu, st_envio


def atualizar_custos(conn, ml_get, agora, orcamento=ORCAMENTO_CUSTOS):
    """Preenche devolucoes.custo_ml com o frete de volta do ML (não mexe no custo digitado à mão)."""
    rows = conn.execute(
        "SELECT id, ml_claim_id FROM devolucoes WHERE ml_claim_id IS NOT NULL AND "
        "(custo_ml_em IS NULL OR (ml_envio IS NULL AND custo_ml_em < ?) "
        "OR (status IN ('esperada','armazem_ml') AND custo_ml_em < ?)) ORDER BY custo_ml_em, id LIMIT ?",
        (agora - 600, agora - RECHECAR_CUSTO_S, orcamento)).fetchall()
    feitos, limite = 0, False
    for r in rows:
        try:
            valor, _, st_envio = _custo_frete_volta(ml_get, r["ml_claim_id"])
        except _LimiteML:
            limite = True
            break
        conn.execute("UPDATE devolucoes SET custo_ml=?, custo_ml_em=?, ml_envio=COALESCE(?, ml_envio) WHERE id=?",
                     (valor, agora, st_envio, r["id"]))
        feitos += 1
    falta = conn.execute("SELECT COUNT(*) AS n FROM devolucoes WHERE ml_claim_id IS NOT NULL AND custo_ml_em IS NULL").fetchone()["n"]
    return feitos, falta, limite


DIAS_FECHADAS = 45
EM_TRANSITO = ("pending", "ready_to_ship", "shipped")
_fechadas_cache = {"em": 0.0, "lista": []}


def _fechadas_recentes(ml_get, agora):
    """Reclamações FECHADAS dos últimos DIAS_FECHADAS dias (a caixa pode ainda estar voltando). Cache de 30 min."""
    if agora - _fechadas_cache["em"] < 1800:
        return _fechadas_cache["lista"]
    corte = time.strftime("%Y-%m-%d", time.localtime(agora - DIAS_FECHADAS * 86400))
    out, off = [], 0
    while off < 1000:
        code, j = ml_get("/post-purchase/v1/claims/search", {"status": "closed", "limit": 50, "offset": off})
        if code != 200 or j is None:
            break
        pg = j.get("data") or []
        out += [c for c in pg if c.get("resource") == "order" and str(c.get("date_created") or "")[:10] >= corte]
        off += 50
        if len(pg) < 50:
            break
    _fechadas_cache.update(em=agora, lista=out)
    return out


def importar_do_ml(ml_get=_ml_get, pausa=0.5) -> dict:
    """Traz para o WMS (status 'esperada') as devoluções ABERTAS do ML que VOLTAM PARA O SELLER (galpão).
    Devolução de produto FULL vai para o armazém do ML e fica de fora. Consulta devagar e lembra o que já checou."""
    garantir_tabelas()
    abertas, off = [], 0
    while off < 200:
        code, j = ml_get("/post-purchase/v1/claims/search", {"status": "opened", "limit": 50, "offset": off})
        if code != 200 or j is None:
            if off == 0:
                return {"ok": False, "erro": "O Mercado Livre não respondeu (token ou permissão). Nada foi importado."}
            break
        pg = j.get("data") or []
        abertas += [c for c in pg if c.get("status") == "opened" and c.get("resource") == "order"]
        off += 50
        if len(pg) < 50:
            break
    agora = int(time.time())
    n_abertas = len(abertas)
    fechadas = _fechadas_recentes(ml_get, agora)
    ids_abertas = {str(c.get("id")) for c in abertas}
    abertas = abertas + [dict(c, _fechada=True) for c in fechadas if str(c.get("id")) not in ids_abertas]
    loja = os.environ.get("DEVOLUCAO_LOJA_ML", "LHV Med")
    novas = ficam_ml = conhecidas = checadas = 0
    parou_429 = False
    erros: list = []
    with db_session() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS devolucoes_ml_vistos (claim_id TEXT PRIMARY KEY, resultado TEXT, atualizado_em BIGINT)")
        for c in abertas:
            cid = str(c.get("id"))
            v = conn.execute("SELECT resultado, atualizado_em FROM devolucoes_ml_vistos WHERE claim_id=?", (cid,)).fetchone()
            tem_linha = conn.execute("SELECT 1 FROM devolucoes WHERE ml_claim_id=?", (cid,)).fetchone() is not None
            if v and ((v["resultado"] in ("importada", "armazem_ml") and tem_linha) or v["resultado"] == "fechada_ok"
                      or (v["resultado"] in ("sem_devolucao",) or str(v["resultado"]).startswith("erro_"))
                      and agora - v["atualizado_em"] < RECHECAR_SEM_DEVOLUCAO_S):
                conhecidas += 1
                continue
            if checadas >= ORCAMENTO_POR_RODADA or parou_429:
                continue
            checadas += 1
            st_envio = None
            if pausa:
                time.sleep(pausa)
            code, ret = ml_get(f"/post-purchase/v2/claims/{cid}/returns")
            if code == 429:
                parou_429 = True
                continue
            if code == 404:
                resultado = "sem_devolucao"
            elif code == 200 and ret is not None:
                tem, destino, st_envio = _destino_da_devolucao(ret)
                resultado = "armazem_ml" if (tem and destino == "warehouse") else ("importada" if tem else "sem_devolucao")
                if c.get("_fechada") and resultado != "sem_devolucao" and st_envio not in EM_TRANSITO:
                    resultado = "fechada_ok"        # reclamação fechada e a caixa já chegou/encerrou: nada a esperar
            else:
                resultado = f"erro_{code}"        # ex.: 403 sem acesso à reclamação; guarda e recheca em 30 min
                erros.append({"claim_id": cid, "pedido": c.get("resource_id"), "http": code,
                              "msg": str(((ret or {}).get("message") or (ret or {}).get("error") or ""))[:120]})
            ex = conn.execute("SELECT id, status, bipado FROM devolucoes WHERE ml_claim_id=?", (cid,)).fetchone()
            if resultado == "armazem_ml":
                ficam_ml += 1
                if ex and ex["status"] == "esperada" and not ex["bipado"]:   # corrige a que entrou como "a caminho do galpão"
                    conn.execute("UPDATE devolucoes SET status='armazem_ml', atualizado_em=? WHERE id=?", (agora, ex["id"]))
            if (resultado in ("importada", "armazem_ml") or resultado.startswith("erro_")) and not ex:
                if pausa:
                    time.sleep(pausa)
                _, ped = ml_get(f"/orders/{c.get('resource_id')}")
                _, cl = ml_get(f"/post-purchase/v1/claims/{cid}")
                d = _dados_do_pedido(ped or {})
                motivo = _motivo_texto(ml_get, (cl or {}).get("reason_id") or c.get("reason_id"))
                sku = d["sku"] or ""
                if sku:
                    p = conn.execute("SELECT sku, nome FROM produtos WHERE LOWER(sku)=LOWER(?) LIMIT 1", (sku,)).fetchone()
                    if p:
                        sku, d["produto"] = p["sku"], p["nome"]
                row = {
                    "criado_em": agora, "atualizado_em": agora, "responsavel": "Mercado Livre",
                    "status": "armazem_ml" if resultado == "armazem_ml" else "esperada",
                    "data_recebimento": time.strftime("%Y-%m-%d"), "loja": loja, "cliente": d["cliente"],
                    "numero_venda": str(c.get("resource_id")), "sku": sku or None,
                    "sku_key": sku[:64].lower() if sku else None, "produto": d["produto"],
                    "qtd": float(d["qtd"] or 1), "valor": d["valor"], "teve_reclamacao": 1,
                    "num_reclamacao": cid, "ml_claim_id": cid, "bipado": 0, "checklist": "{}",
                    "ml_envio": st_envio,
                    "observacoes": f"Importada do Mercado Livre. Motivo ML: {motivo}. Envio de volta: {st_envio or '—'}."
                                   + (" Vai para o ARMAZÉM DO ML (FULL): não passa pelo galpão." if resultado == "armazem_ml" else "")
                                   + (f" ATENÇÃO: o ML não liberou os detalhes desta devolução ({resultado.upper()}): destino NÃO confirmado, confira na chegada." if resultado.startswith("erro_") else ""),
                }
                cols = list(row)
                conn.execute(f"INSERT INTO devolucoes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             tuple(row[k] for k in cols))
                novas += 1
            if v:
                conn.execute("UPDATE devolucoes_ml_vistos SET resultado=?, atualizado_em=? WHERE claim_id=?", (resultado, agora, cid))
            else:
                conn.execute("INSERT INTO devolucoes_ml_vistos (claim_id, resultado, atualizado_em) VALUES (?,?,?)", (cid, resultado, agora))
        custos_feitos, custos_faltam, custos_429 = atualizar_custos(conn, ml_get, agora)
        ids = [str(c.get("id")) for c in abertas]    # abertas + fechadas recentes
        cont = {"importada": 0, "armazem_ml": 0, "sem_devolucao": 0, "erro": 0, "fechada_ok": 0}
        for i in range(0, len(ids), 50):
            lote = ids[i:i + 50]
            for r in conn.execute(f"SELECT resultado, COUNT(*) AS n FROM devolucoes_ml_vistos WHERE claim_id IN ({','.join('?' * len(lote))}) GROUP BY resultado", tuple(lote)).fetchall():
                k = "erro" if str(r["resultado"]).startswith("erro_") else r["resultado"]
                cont[k] = cont.get(k, 0) + r["n"]
    pendentes = max(0, len(abertas) - sum(cont.values()))
    return {"ok": True, "abertas_no_ml": n_abertas, "fechadas_em_transito": len(fechadas), "novas": novas,
            "voltam_pro_galpao": cont["importada"], "ficam_no_ml": cont["armazem_ml"],
            "sem_devolucao": cont["sem_devolucao"], "com_erro": cont["erro"], "erros": erros,
            "pendentes": pendentes, "limite_ml": parou_429 or custos_429,
            "custos_atualizados": custos_feitos, "custos_pendentes": custos_faltam}


@devolucoes_routes.post("/devolucoes/importar-ml")
@login_required
def importar_ml():
    global _ultima_importacao
    agora = time.time()
    if agora - _ultima_importacao < 60:
        return jsonify({"ok": True, "pulado": True, "motivo": "importado há menos de 1 min"})
    _ultima_importacao = agora
    try:
        return jsonify(importar_do_ml())
    except Exception as e:  # noqa: BLE001  nunca derruba a tela
        return jsonify({"ok": False, "erro": f"falha ao importar: {e}"[:300]})
DEVEOF
python3 -m py_compile devolucoes_routes.py && echo ">> devolucoes_routes.py ok"

python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "devolucoes_routes" in s:
    print(">> app.py ja tinha o devolucoes_routes (ok)")
else:
    linha = '    from devolucoes_routes import devolucoes_routes as devolucoes_bp\n    app.register_blueprint(devolucoes_bp, url_prefix="/api")\n'
    ancoras = ['    app.register_blueprint(naoachado_bp, url_prefix="/api")\n',
               '    app.register_blueprint(fefo_bp, url_prefix="/api")\n',
               '    app.register_blueprint(mapa_bp, url_prefix="/api")\n',
               "    app.register_blueprint(api_routes)\n"]
    for a in ancoras:
        if a in s:
            open(f + ".bak_devolucoes", "w", encoding="utf-8").write(s)
            open(f, "w", encoding="utf-8").write(s.replace(a, a + linha, 1))
            print(">> app.py patchado (backup em app.py.bak_devolucoes)")
            break
    else:
        print("!! Nao achei onde registrar. Adicione dentro de create_app():"); print(linha); raise SystemExit(1)
PYEOF
python3 -m py_compile app.py && echo ">> app.py COMPILOU"
echo ""
echo "OK. Reinicie: sudo systemctl restart wms-russia.service ; teste: curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/devolucoes   (deve dar 401)"
echo "Reverter: cp app.py.bak_devolucoes app.py && sudo systemctl restart wms-russia.service   (a tabela devolucoes fica guardada, nao atrapalha)"
echo "SAC: sem SAC_API_URL o envio fica SIMULADO (nada sai). Depois: SAC_API_URL, SAC_API_TOKEN, SAC_DEVOLUCAO_PATH no drop-in do servico."
