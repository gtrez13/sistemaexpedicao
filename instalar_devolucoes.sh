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
import re
import threading
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
             ("ml_review", "TEXT"), ("ml_review_em", "BIGINT"), ("ml_review_por", "TEXT"), ("ml_review_msg", "TEXT"),
             ("full_situacao", "TEXT"), ("full_info", "TEXT"), ("full_em", "BIGINT"),
             ("canal", "TEXT"), ("servico", "TEXT"), ("embalagem", "TEXT"), ("estado_produto", "TEXT"),
             ("nf_dev_id", "TEXT"), ("nf_dev_numero", "TEXT"), ("nf_dev_deposito", "TEXT"), ("nf_dev_por", "TEXT"),
             ("nf_dev_em", "BIGINT"), ("nf_dev_msg", "TEXT"),
             ("nf_dev_etapa", "TEXT"), ("nf_dev_etapa_em", "BIGINT"), ("nf_dev_situacao", "TEXT"), ("nf_dev_erro", "TEXT"),
             ("nf_dev_conf", "TEXT")]
COLS_TXT = ["data_recebimento", "loja", "cliente", "numero_venda", "sku", "produto", "serie_lote", "nf_saida",
            "num_reclamacao", "motivo", "alegacao_defeito", "desc_avaria", "chegada", "qual_item", "destino",
            "observacoes", "status", "canal", "servico", "embalagem", "estado_produto"]
COLS_BOOL = ["teve_reclamacao", "avaria", "faltou_item", "volta_estoque"]


_tab_lock = threading.Lock()   # várias telas abrindo juntas depois de reiniciar = ALTER TABLE ao mesmo tempo = deadlock


def garantir_tabelas(conn=None):
    global _tabelas_ok
    if _tabelas_ok:
        return
    with _tab_lock:
        if _tabelas_ok:
            return
        _garantir_tabelas(conn)


def _garantir_tabelas(conn=None):
    global _tabelas_ok

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
            # só altera o que falta (cada ALTER trava a tabela inteira até o fim da transação)
            have = {str(r["column_name"]) for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='devolucoes'").fetchall()}
            for col, tp in [("ml_claim_id", "TEXT"), ("custo_devolucao", "DOUBLE PRECISION"), ("custo_ml", "DOUBLE PRECISION"),
                            ("custo_ml_em", "BIGINT"), ("ml_envio", "TEXT")] + list(EXTRAS_V3):
                if col not in have:
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
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS devolucao_videos (
                id            {pk},
                devolucao_id  BIGINT NOT NULL,
                arquivo       TEXT NOT NULL,
                mime          TEXT,
                duracao       REAL,
                tamanho       BIGINT,
                criado_em     BIGINT NOT NULL,
                criado_por    TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_devolucao_videos ON devolucao_videos (devolucao_id)")
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS devolucao_nf_log (
                id            {pk},
                devolucao_id  BIGINT NOT NULL,
                acao          TEXT NOT NULL,
                ok            INTEGER,
                origem        TEXT,
                http          INTEGER,
                mensagem      TEXT,
                nf_id         TEXT,
                resposta      TEXT,
                enviado       TEXT,
                por           TEXT,
                em            BIGINT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_devolucao_nf_log ON devolucao_nf_log (devolucao_id, id)")
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
            if campo == "nf_saida" and r["ml_claim_id"]:
                continue        # devolução do ML: a NF de saída não é exigida na conferência (prancheta)
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
        d["videos"] = [{"id": x["id"], "duracao": x["duracao"], "por": x["criado_por"], "em": x["criado_em"]}
                       for x in conn.execute("SELECT id, duracao, criado_por, criado_em FROM devolucao_videos WHERE devolucao_id=? ORDER BY id", (did,)).fetchall()]
    d["nf_dev_sugerido"] = _deposito_sugerido(r)
    for k in ("nf_dev_id", "nf_dev_numero", "nf_dev_deposito", "nf_dev_por", "nf_dev_em", "nf_dev_msg", "nf_dev_situacao", "nf_dev_etapa"):
        d[k] = r[k] if k in r.keys() else None
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
        conn.execute(f"UPDATE devolucoes SET {sets} WHERE id=?", tuple(novos.values()) + (did,))
        _recalcular(conn, did)
        row = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        st = row["status"]
    sac = None
    # chegou fora do esperado (avaria, faltou item, diferente, usado...) e já foi bipado: abre O.S. no SAC sozinho (uma vez)
    if row["bipado"] and tem_problema(row) and row["chegada"] != CHEGADAS[1] and not row["sac_os_id"]:
        s_st, s_msg, s_os = enviar_ao_sac(row)
        with db_session() as conn:
            conn.execute("UPDATE devolucoes SET sac_status=?, sac_msg=?, sac_os_id=?, sac_enviado_em=? WHERE id=?",
                         ("enviado" if s_st == "enviado" else ("pendente" if s_st == "simulado" else "erro"),
                          s_msg, s_os, int(time.time()) if s_st == "enviado" else None, did))
        sac = {"status": s_st, "msg": s_msg, "os_id": s_os}
    return jsonify({"success": True, "status": st, "sac": sac})


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


MAX_VIDEO = 60 * 1024 * 1024
VIDEO_MIN_S = 5


def _tipo_video(b: bytes):
    if b[4:8] == b"ftyp":
        return ("mov", "video/quicktime") if b[8:10] == b"qt" else ("mp4", "video/mp4")
    if b[:4] == b"\x1a\x45\xdf\xa3":
        return "webm", "video/webm"
    return None, None


@devolucoes_routes.post("/devolucoes/<int:did>/video")
@login_required
def video_enviar(did):
    """Vídeo da conferência (mínimo 5 s, gravado na prancheta)."""
    f = request.files.get("video")
    if not f:
        return jsonify({"error": "Envie o vídeo (campo 'video')."}), 400
    try:
        dur = float(request.form.get("duracao") or 0)
    except ValueError:
        dur = 0
    if dur < VIDEO_MIN_S:
        return jsonify({"error": f"O vídeo precisa ter pelo menos {VIDEO_MIN_S} segundos."}), 400
    dados = f.read(MAX_VIDEO + 1)
    if len(dados) > MAX_VIDEO:
        return jsonify({"error": "Vídeo grande demais (máx. 60 MB)."}), 413
    ext, mime = _tipo_video(dados)
    if not ext:
        return jsonify({"error": "Arquivo não é um vídeo (MP4, MOV ou WEBM)."}), 400
    garantir_tabelas()
    with db_session() as conn:
        if not conn.execute("SELECT 1 FROM devolucoes WHERE id=?", (did,)).fetchone():
            return jsonify({"error": "Devolução não encontrada."}), 404
        nome = f"{did}_video_{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(_pasta_fotos(), nome), "wb") as out:
            out.write(dados)
        conn.execute("INSERT INTO devolucao_videos (devolucao_id, arquivo, mime, duracao, tamanho, criado_em, criado_por) VALUES (?,?,?,?,?,?,?)",
                     (did, nome, mime, dur, len(dados), int(time.time()), _quem()))
        vid = conn.execute("SELECT id FROM devolucao_videos WHERE arquivo=?", (nome,)).fetchone()["id"]
    return jsonify({"success": True, "id": vid, "duracao": dur})


@devolucoes_routes.get("/devolucoes/videos/<int:vid>")
@login_required
def video_ver(vid):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT arquivo, mime FROM devolucao_videos WHERE id=?", (vid,)).fetchone()
    if not r:
        return jsonify({"error": "Vídeo não encontrado."}), 404
    caminho = os.path.join(_pasta_fotos(), os.path.basename(r["arquivo"]))
    if not os.path.exists(caminho):
        return jsonify({"error": "Arquivo do vídeo não encontrado."}), 404
    return send_file(caminho, mimetype=r["mime"] or "video/mp4", conditional=True, max_age=86400)


@devolucoes_routes.delete("/devolucoes/videos/<int:vid>")
@login_required
def video_apagar(vid):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT v.arquivo, d.status FROM devolucao_videos v JOIN devolucoes d ON d.id=v.devolucao_id WHERE v.id=?", (vid,)).fetchone()
        if not r:
            return jsonify({"error": "Vídeo não encontrado."}), 404
        if r["status"] == "finalizada" and not eh_gestor():
            return jsonify({"error": "Devolução finalizada: só supervisor apaga."}), 403
        conn.execute("DELETE FROM devolucao_videos WHERE id=?", (vid,))
    try:
        os.remove(os.path.join(_pasta_fotos(), os.path.basename(r["arquivo"])))
    except OSError:
        pass
    return jsonify({"success": True})


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


def _ml_review_post(claim, tipo, **kw):
    """Revisão da devolução. Conta local (MLB) usa /post-purchase/v1; /marketplace/v2 é do Global Selling e dá 403 aqui.
    Só tenta o 2º caminho se o 1º nem existir para a conta (403/404/405) — nesses casos o ML não fez nada."""
    primeiro = None
    for base in ("/post-purchase/v1", "/marketplace/v2"):
        st, resp = _ml_req("POST", f"{base}/claims/{claim}/actions/return-review-{tipo}", **kw)
        print(f"[ML-REVISAO] POST {base}/claims/{claim}/actions/return-review-{tipo} -> {st} {str(resp)[:300]}", flush=True)
        if st not in (403, 404, 405):
            return st, resp
        primeiro = primeiro or (st, resp)
    return primeiro


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
    st, claim = _ml_req("GET", f"/post-purchase/v1/claims/{r['ml_claim_id']}")
    if st != 200:
        st, claim = _ml_req("GET", f"/marketplace/v2/claims/{r['ml_claim_id']}")
    if st != 200:
        out["aviso"] = f"ML respondeu {st} ao consultar a reclamação: {str(claim)[:300]}"
        return jsonify(out)
    acoes = _acoes_seller(claim)
    out.update({"pode_ok": "return_review_ok" in acoes, "pode_fail": "return_review_fail" in acoes,
                "acoes": acoes, "status_claim": claim.get("status"), "stage": claim.get("stage")})
    st2, mot = _ml_req("GET", "/post-purchase/v1/returns/reasons", params={"flow": "seller_return_failed", "claim_id": r["ml_claim_id"]})
    if st2 != 200:
        st2, mot = _ml_req("GET", "/marketplace/v2/returns/reasons/return-fail")
    if isinstance(mot, dict):
        mot = mot.get("reasons") or mot.get("results") or mot.get("data") or []
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
        st, resp = _ml_review_post(claim, "ok")
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
        st, resp = _ml_review_post(claim, "fail", json={"reason": motivo, "message": mensagem, "attachments": anexos})
        resultado, msg = "problema", f"{motivo}: {mensagem}" + (f" ({len(anexos)} foto(s))" if anexos else "")
    if st not in (200, 201):
        # 422 (e não 502): a Cloudflare troca qualquer 502 pela página dela e a mensagem do ML se perde
        det = resp.get("message") or resp.get("error") or resp if isinstance(resp, dict) else resp
        print(f"[ML-REVISAO] devolucao={did} claim={claim} ok={bool(b.get('ok'))} ML {st}: {str(resp)[:800]}", flush=True)
        dica = {429: "O ML está limitando as chamadas agora. Espere 1 minuto e tente de novo.",
                401: "O token do ML venceu e não renovou.",
                403: "O ML não deixou a aplicação do WMS fazer esta ação (permissão da aplicação no ML)."}.get(st, "")
        return jsonify({"error": f"O ML recusou ({st}): {str(det)[:400]}" + (f" — {dica}" if dica else ""), "ml_status": st}), 422
    print(f"[ML-REVISAO] devolucao={did} claim={claim} {resultado} enviado ao ML ({st})", flush=True)
    agora = int(time.time())
    with db_session() as conn:
        conn.execute("UPDATE devolucoes SET ml_review=?, ml_review_em=?, ml_review_por=?, ml_review_msg=?, atualizado_em=? WHERE id=?",
                     (resultado, agora, _quem(), msg, agora, did))
    return jsonify({"success": True, "resultado": resultado, "msg": msg})


# ───────────────────────────── PRANCHETA: preenche sozinho o que dá (igual ao Forms de devolução) ─────────────────────────────
EMPRESAS_FORMS = ["MEDICAL", "LHVMED", "CONTA DA TICI", "CONTA DA SIMONE", "CONTA DO DAVID"]
SERVICOS = ["Devolução", "Troca", "Conserto", "Retorno (exposição / empréstimo)"]


def _nf_saida_bling(numero_venda):
    """NF de saída pelo Bling (a LHVMED emite lá; o ML só conhece a nota se ela foi enviada para ele).
    Pedido de venda do Bling com numeroLoja = venda (ou pacote) do ML -> nota fiscal ligada. -> (nf|None, fonte/aviso)"""
    nf, info, _ = _venda_bling(numero_venda)
    return nf, info


def _venda_bling(numero_venda):
    """-> (nº 'NUM/SERIE' | None, fonte/aviso, {"pv": detalhe do pedido, "nf": detalhe da NF de venda} | None)"""
    from datetime import datetime, timedelta
    from services.bling_service import BlingService
    venda = str(numero_venda or "").strip()
    alvos, data_venda = [venda], None
    st, o = _ml_req("GET", f"/orders/{venda}")
    if st != 200:                                   # pode ser o número do pacote
        sp, pk = _ml_req("GET", f"/packs/{venda}")
        if sp == 200 and isinstance(pk, dict) and pk.get("orders"):
            oid = str(pk["orders"][0].get("id"))
            alvos.append(oid)
            st, o = _ml_req("GET", f"/orders/{oid}")
    if st == 200 and isinstance(o, dict):
        if o.get("pack_id"):
            alvos.append(str(o["pack_id"]))
        data_venda = str(o.get("date_created") or "")[:10] or None
    alvos = list(dict.fromkeys(a for a in alvos if a))
    b = BlingService()
    pv_id = None
    for a in alvos:                                  # 1) filtro direto pelo número da loja
        try:
            d = b._get_json("/pedidos/vendas", params={"numerosLojas[]": a, "limite": 100}, timeout=(10, 30))
            pv_id = next((x.get("id") for x in d.get("data") or [] if str(x.get("numeroLoja") or "").strip() in alvos), None)
        except Exception as e:
            print(f"[NF-SAIDA] bling numerosLojas {a}: {e}", flush=True)
        if pv_id:
            break
    if not pv_id and data_venda:                     # 2) pedidos do dia da venda (até 5 páginas)
        try:
            dv = datetime.strptime(data_venda, "%Y-%m-%d")
            ini, fim = (dv - timedelta(days=1)).strftime("%Y-%m-%d"), (dv + timedelta(days=2)).strftime("%Y-%m-%d")
            for pag in range(1, 6):
                d = b._get_json("/pedidos/vendas", params={"dataInicial": ini, "dataFinal": fim, "pagina": pag, "limite": 100}, timeout=(10, 30))
                itens = d.get("data") or []
                pv_id = next((x.get("id") for x in itens if str(x.get("numeroLoja") or "").strip() in alvos), None)
                if pv_id or len(itens) < 100:
                    break
        except Exception as e:
            print(f"[NF-SAIDA] bling por data {data_venda}: {e}", flush=True)
    print(f"[NF-SAIDA] venda {venda} alvos={alvos} data={data_venda} -> pedido Bling {pv_id}", flush=True)
    if not pv_id:
        return None, f"NF de saída: não achei a venda {venda} no Bling (preencha à mão)", None
    det = b.get_pedido_venda_detalhe(int(pv_id))
    det = det.get("data") or det
    nf_id = (det.get("notaFiscal") or {}).get("id")
    if not nf_id:
        return None, f"NF de saída: o pedido {det.get('numero')} do Bling ainda não tem nota fiscal (preencha à mão)", {"pv": det, "nf": None}
    nf = b._get_json(f"/nfe/{int(nf_id)}").get("data") or {}
    num = str(nf.get("numero") or "").strip().lstrip("0")
    if not num:
        return None, "NF de saída: o Bling não devolveu o número da nota (preencha à mão)", {"pv": det, "nf": nf}
    serie = str(nf.get("serie") or "").strip()
    return num + (f"/{serie}" if serie else ""), f"nota fiscal do pedido {det.get('numero')} no Bling", {"pv": det, "nf": nf}


# ───────────────────────── NOTA DE DEVOLUÇÃO NO BLING (fica PENDENTE, não vai à SEFAZ) ─────────────────────────
# Fluxo: _plano_nf_dev monta TUDO sem gravar nada (é a "prévia"/teste); nf_dev_gerar usa o mesmo plano e cria.
DEPOSITO_LOCAL = os.environ.get("BLING_DEPOSITO_LOCAL", "Local")
DEPOSITO_DEFEITOS = os.environ.get("BLING_DEPOSITO_DEFEITOS", "Defeitos / Garantia")
NATUREZA_DEVOLUCAO = os.environ.get("BLING_NATUREZA_DEVOLUCAO", "Devolução de mercadorias")          # cliente com IE (contribuinte)
# cliente CPF ou sem IE = não contribuinte / consumidor final -> naturezas próprias do Bling da LHVMED (dentro / fora de SP)
NATUREZA_DEV_NC_SP = os.environ.get("BLING_NATUREZA_DEV_NC_SP", "15101723921")
NATUREZA_DEV_NC_FORA = os.environ.get("BLING_NATUREZA_DEV_NC_FORA", "15101723906")
NF_DEV_EMAIL_PADRAO = os.environ.get("NF_DEV_EMAIL_PADRAO", "").strip()        # usado só se nem o Bling nem o ML tiverem
NF_DEV_TELEFONE_PADRAO = os.environ.get("NF_DEV_TELEFONE_PADRAO", "").strip()
NOME_DEP = {"LOCAL": "Local", "DEFEITOS": "Defeitos / Garantia"}
SIT_NF = {1: "Pendente", 2: "Cancelada", 3: "Aguardando recibo", 4: "Rejeitada", 5: "Autorizada", 6: "Emitida DANFE",
          7: "Registrada", 8: "Aguardando protocolo", 9: "Denegada", 10: "Consulta situação", 11: "Bloqueada"}


def _norm_txt(t):
    import unicodedata
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()).strip()


def _so_dig(x):
    return re.sub(r"\D", "", str(x or ""))


def _id_por_nome(b, caminho, nome):
    if str(nome).isdigit():
        return int(nome)
    for x in (b._get_json(caminho, params={"limite": 100}).get("data") or []):
        if _norm_txt(x.get("descricao")) == _norm_txt(nome):
            return int(x["id"])
    return None


def _deposito_sugerido(r):
    """LOCAL | DEFEITOS | None (= o operador escolhe). Regras do Biel."""
    ch = str(r["chegada"] or "")
    emb = _norm_txt(r["embalagem"])
    if r["avaria"] or r["faltou_item"] or ch in ("Produto danificado", "Produto diferente do enviado"):
        return "DEFEITOS"
    if ch == "Produto usado ou com sinais de uso" or "danific" in emb or "violad" in emb or ch == "Embalagem danificada, produto intacto":
        return None
    if ch == "Correto e sem danos aparentes":
        return "LOCAL"
    return None


def _bling_uma_vez(b, metodo, caminho, payload=None, params=None):
    """Chamada ao Bling SEM retry (o _request do serviço repete em 5xx e poderia criar 2 notas). -> (status, texto, json|None)"""
    import requests
    try:
        rr = requests.request(metodo, f"{b.base_url}{caminho}", headers=b._headers(), json=payload, params=params, timeout=(10, 60))
    except Exception as e:
        return 0, f"sem resposta do Bling: {e}", None
    try:
        js = rr.json()
    except Exception:
        js = None
    return rr.status_code, (rr.text or "")[:800], js


def _msg_bling(txt):
    """Tira a mensagem legível do erro do Bling (inclui os campos com problema)."""
    try:
        e = json.loads(txt).get("error") or {}
        partes = [e.get("description") or e.get("message") or ""]
        for f in e.get("fields") or []:
            partes.append(f"{f.get('element') or ''}: {f.get('msg') or ''}".strip(": "))
        return " | ".join(p for p in partes if p)[:400]
    except Exception:
        return str(txt or "")[:400]


def _quem_seguro():
    try:
        return _quem()
    except Exception:
        return ""


def _nf_log(did, acao, ok, origem=None, http=None, mensagem="", nf_id=None, resposta="", enviado=None, por=None):
    """Prova do que aconteceu: cada chamada ao Bling fica gravada (o que foi enviado e o que o Bling respondeu)."""
    try:
        with db_session() as conn:
            conn.execute("INSERT INTO devolucao_nf_log (devolucao_id, acao, ok, origem, http, mensagem, nf_id, resposta, enviado, por, em) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (did, acao, 1 if ok else 0, origem, http, str(mensagem or "")[:1000], str(nf_id) if nf_id else None,
                          str(resposta or "")[:4000], json.dumps(enviado, ensure_ascii=False)[:8000] if enviado is not None else None,
                          por if por is not None else _quem_seguro(), int(time.time())))
    except Exception as e:
        print(f"[NF-DEV] log falhou ({did}): {e}", flush=True)


def _campos_bling(txt):
    try:
        e = json.loads(txt).get("error") or {}
        return [{"campo": f.get("element") or "", "msg": f.get("msg") or ""} for f in e.get("fields") or []]
    except Exception:
        return []


def _erro_exc(e, onde):
    """Exceção -> (origem, mensagem). O BlingService levanta 'Bling GET falhou: <status> - <json>'."""
    t = str(e)
    if "Bling" in t:
        m = re.search(r"falhou:\s*(\d{3})\s*-\s*(.*)", t, re.S)
        return "bling", (f"O Bling respondeu erro ao {onde} (HTTP {m.group(1)}): {_msg_bling(m.group(2))}" if m
                         else f"O Bling respondeu erro ao {onde}: {t[:300]}")
    return "wms", f"Erro interno do WMS ao {onde} ({type(e).__name__}): {t[:300]}"


def _erro(origem, msg, status=422, **extra):
    """Erro com a ORIGEM clara para a tela: bling | ml | wms | conferencia."""
    return jsonify({"error": msg, "origem": origem, **extra}), status


@devolucoes_routes.record_once
def _handler_global(state):
    """Erro interno não tratado em QUALQUER rota do WMS: devolve JSON com o erro de verdade (e não página HTML),
    com um código de referência para achar no log. Não mexe em 401/404/405 (HTTPException)."""
    from werkzeug.exceptions import HTTPException
    app = state.app
    if Exception in (app.error_handler_spec.get(None, {}).get(None, {}) or {}):
        return   # o app já tem um tratador próprio: não troca

    def _tratar(e):
        if isinstance(e, HTTPException):
            return e
        import traceback
        ref = uuid.uuid4().hex[:8]
        print(f"[ERRO-WMS] ref {ref} {request.method} {request.path}\n{traceback.format_exc()}", flush=True)
        return jsonify({"error": f"Erro interno do WMS ({type(e).__name__}): {str(e)[:300]} · ref {ref}",
                        "origem": "wms", "ref": ref}), 500
    app.register_error_handler(Exception, _tratar)


def _cfop_devolucao(cfop_venda):
    c = _so_dig(cfop_venda)
    if len(c) != 4:
        return None
    pref = {"5": "1", "6": "2", "7": "3"}.get(c[0])
    if not pref:
        return None
    return pref + ("411" if c[1:] in ("405", "403", "401") else "202")


def _cliente_ml(numero_venda):
    """O que o ML entrega do comprador: telefone e endereço (envio), CPF/CNPJ (faturamento) e, em contas antigas, e-mail.
    -> (dados, fonte_por_campo, aviso|None). Só leitura."""
    out, fonte = {}, {}
    venda = str(numero_venda or "").strip()
    try:
        st, o = _ml_req("GET", f"/orders/{venda}")
        if st != 200:
            sp, pk = _ml_req("GET", f"/packs/{venda}")
            if sp == 200 and isinstance(pk, dict) and pk.get("orders"):
                st, o = _ml_req("GET", f"/orders/{pk['orders'][0].get('id')}")
        if st != 200 or not isinstance(o, dict):
            return out, fonte, f"o ML não devolveu a venda {venda} (HTTP {st})"
        bu = o.get("buyer") or {}
        em = str(bu.get("email") or "").strip()
        if "@" in em:
            out["email"], fonte["email"] = em, "comprador no ML"
        ph = bu.get("phone") or {}
        tel = _so_dig(f"{ph.get('area_code') or ''}{ph.get('number') or ''}")
        if len(tel) >= 10:
            out["telefone"], fonte["telefone"] = tel, "comprador no ML"
        sid = (o.get("shipping") or {}).get("id")
        if sid:
            ss, sh = _ml_req("GET", f"/shipments/{sid}")
            if ss == 200 and isinstance(sh, dict):
                ra = sh.get("receiver_address") or {}
                tel = _so_dig(ra.get("receiver_phone"))
                if len(tel) >= 10 and "telefone" not in out:
                    out["telefone"], fonte["telefone"] = tel, "endereço de entrega no ML"
                end = {"endereco": ra.get("street_name"), "numero": ra.get("street_number"), "complemento": ra.get("comment"),
                       "bairro": (ra.get("neighborhood") or {}).get("name"), "cep": _so_dig(ra.get("zip_code")),
                       "municipio": (ra.get("city") or {}).get("name"),
                       "uf": str((ra.get("state") or {}).get("id") or "").replace("BR-", "")[:2]}
                out["endereco"] = {k: str(v) for k, v in end.items() if v}
                if ra.get("receiver_name"):
                    out["nome"] = ra["receiver_name"]
        bs, bi = _ml_req("GET", f"/orders/{o.get('id')}/billing_info")
        if bs == 200 and isinstance(bi, dict):
            doc = _so_dig((bi.get("billing_info") or {}).get("doc_number"))
            if doc:
                out["numeroDocumento"] = doc
        return out, fonte, None
    except Exception as e:
        return out, fonte, f"erro lendo o ML: {e}"


def _plano_nf_dev(r, dep_pedido=None, checar_duplicata=True):
    """Monta a nota de devolução SEM gravar nada (no WMS, no Bling ou no ML).
    -> {"pontos": [{ok: True|False|None, item, detalhe}], "bloqueio": str|None, "payloads": [...], ...}
       ok=False bloqueia; ok=None é aviso (cria, mas confira no Bling antes de enviar)."""
    from services.bling_service import BlingService
    pontos = []
    plano = {"pontos": pontos, "bloqueio": None, "payloads": [], "dep": None, "dep_id": None, "sugerido": None,
             "duplicada": None, "venda_nf": None}

    def ponto(ok, item, det=""):
        pontos.append({"ok": ok, "item": item, "detalhe": det})
        if ok is False and not plano["bloqueio"]:
            plano["bloqueio"] = f"{item}: {det}"

    if r["status"] == "armazem_ml":
        ponto(False, "FULL", "devolução FULL volta para o armazém do ML; o WMS não gera nota")
        return plano
    if r["nf_dev_numero"] or r["nf_dev_id"]:
        ponto(False, "Nota de devolução", f"já existe: NF {r['nf_dev_numero'] or r['nf_dev_id']}")
        return plano
    if not r["bipado"] or not r["chegada"]:
        ponto(False, "Conferência", "conclua a prancheta antes (bipar, caixa, produto)")
    else:
        ponto(True, "Conferência", f"{r['chegada']} · caixa: {r['embalagem'] or '-'}")

    sug = _deposito_sugerido(r)
    dep = str(dep_pedido or "").upper() or sug
    plano["sugerido"], plano["dep"] = sug, dep
    if sug == "DEFEITOS" and dep == "LOCAL":
        ponto(False, "Depósito", "produto com problema não volta para o Local: vai para Defeitos / Garantia")
    elif dep in NOME_DEP:
        ponto(True, "Depósito", NOME_DEP[dep] + (" (sugerido)" if dep == sug else " (escolha do operador)"))
    else:
        ponto(None, "Depósito", "caixa danificada, usado ou outro motivo: o operador escolhe Local ou Defeitos")

    nfv, info, ctx = _venda_bling(r["numero_venda"])
    venda, pv = (ctx or {}).get("nf"), (ctx or {}).get("pv") or {}
    if not venda:
        ponto(False, "NF de venda no Bling", info)
        return plano
    plano["venda_nf"] = nfv
    sit = int(venda.get("situacao") or 0)
    if sit in (1, 2, 4, 9, 11):
        ponto(False, "NF de venda no Bling", f"NF {nfv} está '{SIT_NF.get(sit, sit)}': só dá para devolver nota autorizada")
    else:
        ponto(True, "NF de venda no Bling", f"NF {nfv} ({SIT_NF.get(sit, sit)}) · pedido {pv.get('numero') or '-'}")
    chave = _so_dig(venda.get("chaveAcesso"))
    ponto(True if len(chave) == 44 else False, "Chave da NF de venda", chave or "sem chave de acesso")

    sku = str(r["sku"] or "").strip().upper()
    item = next((it for it in venda.get("itens") or []
                 if sku and sku in (str(it.get("codigo") or "").strip().upper(), str(it.get("gtin") or "").strip().upper())), None)
    if not item:
        cods = ", ".join(str(it.get("codigo")) for it in venda.get("itens") or [])
        ponto(False, "SKU", f"o SKU bipado ({r['sku']}) não está na NF {nfv} (itens: {cods})")
        return plano
    ponto(True, "SKU", f"{item.get('codigo')} · {item.get('descricao')}")
    qtd, vendida = float(r["qtd"] or 1), float(item.get("quantidade") or 0)
    ponto(qtd <= vendida + 1e-6, "Quantidade", f"devolvida {qtd:g} · vendida {vendida:g}")
    cfop = _cfop_devolucao(item.get("cfop"))
    ponto(True if cfop else False, "CFOP", f"venda {item.get('cfop')} -> devolução {cfop}" if cfop else f"não sei o CFOP de devolução para {item.get('cfop')}")

    b = BlingService()
    if dep in NOME_DEP:
        err_dep = None
        try:
            plano["dep_id"] = _id_por_nome(b, "/depositos", DEPOSITO_LOCAL if dep == "LOCAL" else DEPOSITO_DEFEITOS)
        except Exception as e:
            print(f"[NF-DEV] depósitos: {e}", flush=True)
            err_dep = _erro_exc(e, "buscar os depósitos")[1]
            plano.setdefault("bloqueio_origem", "bling")
        if not plano["dep_id"]:
            ponto(False, "Depósito no Bling", err_dep or f"o depósito '{NOME_DEP[dep]}' não existe no Bling")

    # cliente: o que falta no cadastro (Bling) vem do ML; e-mail/telefone sem fonte usam o padrão (se configurado)
    cont = {k: v for k, v in dict(venda.get("contato") or {}).items()}
    end = dict(cont.get("endereco") or {})
    ml, fonte_ml, aviso_ml = ({}, {}, None)
    faltando = [c for c in ("email", "telefone") if not str(cont.get(c) or "").strip()] + \
               [c for c in ("endereco", "numero", "bairro", "cep", "municipio", "uf") if not str(end.get(c) or "").strip()]
    if faltando or not _so_dig(cont.get("numeroDocumento")):
        ml, fonte_ml, aviso_ml = _cliente_ml(r["numero_venda"])
    principais = ("endereco", "numero", "bairro", "cep", "municipio", "uf")
    end_ml = ml.get("endereco") or {}
    end_veio_ml = False
    if any(not str(end.get(c) or "").strip() for c in principais) and all(str(end_ml.get(c) or "").strip() for c in principais):
        end = {**end_ml, "pais": end.get("pais") or ""}     # endereço inteiro do ML (não mistura cidade de um com UF do outro)
        end = {k: v for k, v in end.items() if v}
        end_veio_ml = True
    cont["endereco"] = end
    doc_veio_ml = False
    if not _so_dig(cont.get("numeroDocumento")) and ml.get("numeroDocumento"):
        cont["numeroDocumento"] = ml["numeroDocumento"]
        doc_veio_ml = True
    doc = _so_dig(cont.get("numeroDocumento"))
    cont.setdefault("tipoPessoa", "J" if len(doc) == 14 else "F")
    if not cont.get("ie"):
        cont.setdefault("contribuinte", 9)
    ponto(True if cont.get("nome") else False, "Cliente", f"{cont.get('nome') or 'sem nome'} · doc {doc or 'FALTA'}")
    t_wms = {w for w in _norm_txt(r["cliente"]).split() if len(w) > 2}
    t_nf = {w for w in _norm_txt(cont.get("nome")).split() if len(w) > 2}
    if t_wms and t_nf and not (t_wms & t_nf):
        ponto(None, "Nome diferente", f"na devolução está '{r['cliente']}', na NF de venda '{cont.get('nome')}' (quem recebeu x quem comprou). "
                                      "A nota sai no nome da NF de venda, que é o certo; só confira se é a venda certa")
    ponto(True if len(doc) in (11, 14) else False, "CPF/CNPJ do cliente",
          (doc + (" (veio do ML)" if doc_veio_ml else " (cadastro do Bling)")) if doc else "falta (nem o Bling nem o ML têm)")
    falta_end = [c for c in principais if not str(end.get(c) or "").strip()]
    ponto(None if falta_end else True, "Endereço do cliente",
          (f"falta: {', '.join(falta_end)} (nem o Bling nem o ML têm completo)" if falta_end
           else f"{end.get('endereco')}, {end.get('numero')} · {end.get('municipio')}/{end.get('uf')}")
          + (" (veio do ML: o cadastro do Bling estava incompleto)" if end_veio_ml else ""))
    for campo, nome, padrao in (("email", "E-mail do cliente", os.environ.get("NF_DEV_EMAIL_PADRAO", NF_DEV_EMAIL_PADRAO).strip()),
                                ("telefone", "Telefone do cliente", os.environ.get("NF_DEV_TELEFONE_PADRAO", NF_DEV_TELEFONE_PADRAO).strip())):
        v = str(cont.get(campo) or "").strip()
        if v:
            ponto(True, nome, f"{v} (cadastro do Bling)")
        elif ml.get(campo):
            cont[campo] = ml[campo]
            ponto(True, nome, f"{ml[campo]} (veio do ML: {fonte_ml.get(campo)})")
        elif padrao:
            cont[campo] = padrao
            ponto(True, nome, f"{padrao} (padrão: o cliente não tem no Bling nem no ML)")
        else:
            ponto(None, nome, "falta no Bling e o ML não informa" + (f" ({aviso_ml})" if aviso_ml else "")
                  + ": complete no Bling antes de enviar, ou configure um padrão")
    plano["contato"] = cont

    # natureza pelo tipo de cliente: CPF / sem IE = não contribuinte (consumidor final); dentro ou fora de SP pelo CFOP da venda
    ie = str(cont.get("ie") or "").strip().upper()
    tem_ie = bool(_so_dig(ie)) and ie not in ("ISENTO", "ISENTA")
    nao_contrib = len(doc) == 11 or not tem_ie
    cf_venda = _so_dig(item.get("cfop"))
    dentro_sp = cf_venda.startswith("5") if cf_venda else str(end.get("uf") or "").upper() == "SP"
    if nao_contrib:
        cont["contribuinte"] = 9
        nat_alvo = NATUREZA_DEV_NC_SP if dentro_sp else NATUREZA_DEV_NC_FORA
        por_que = f"cliente {'CPF' if len(doc) == 11 else 'sem IE'} = não contribuinte / consumidor final, {'dentro de SP' if dentro_sp else 'fora de SP'}"
    else:
        nat_alvo = NATUREZA_DEVOLUCAO
        por_que = "cliente com IE = contribuinte"
        if os.environ.get("NF_DEV_CONTRIBUINTE_GERAR", "0") != "1":
            ponto(False, "Cliente contribuinte", f"tem inscrição estadual ({ie}): quem emite a nota de devolução é o CLIENTE. "
                                                 "Peça a NF de devolução dele; quando ela entrar no Bling, o WMS liga aqui")
    nat, nat_desc, err_nat = None, nat_alvo, None
    try:
        for x in (b._get_json("/naturezas-operacoes", params={"limite": 100}).get("data") or []):
            if str(x.get("id")) == str(nat_alvo) or _norm_txt(x.get("descricao")) == _norm_txt(nat_alvo):
                nat, nat_desc = int(x["id"]), str(x.get("descricao") or "").strip()
                break
    except Exception as e:
        print(f"[NF-DEV] naturezas: {e}", flush=True)
        err_nat = _erro_exc(e, "buscar as naturezas de operação")[1]
        plano.setdefault("bloqueio_origem", "bling")
    plano["natureza"] = nat_desc
    ponto(True if nat else False, "Natureza de operação",
          f"{nat_desc} ({nat}) · {por_que}" if nat else (err_nat or f"a natureza '{nat_alvo}' não existe no Bling ({por_que})"))

    # nota duplicada? (nota de entrada do mesmo cliente com esse item, depois da venda)
    if checar_duplicata:
        try:
            ini = str(venda.get("dataEmissao") or "")[:10]
            for pag in range(1, 4):
                lst = b._get_json("/nfe", params={"tipo": 0, "dataEmissaoInicial": ini, "pagina": pag, "limite": 100}).get("data") or []
                for n in lst:
                    if str((n.get("contato") or {}).get("id")) != str((venda.get("contato") or {}).get("id")):
                        continue
                    if str(n.get("id")) == str(venda.get("id")):
                        continue
                    dn = b._get_json(f"/nfe/{n['id']}").get("data") or {}
                    if str(dn.get("tipo")) != "0" or int(dn.get("situacao") or 0) in (2, 9):   # só ENTRADA válida (não cancelada/denegada)
                        continue
                    desc_v, gtin_v = _norm_txt(item.get("descricao")), str(item.get("gtin") or "").strip()
                    if any(str(i.get("codigo") or "").strip().upper() == sku or (desc_v and _norm_txt(i.get("descricao")) == desc_v)
                           or (gtin_v and str(i.get("gtin") or "").strip() == gtin_v) for i in dn.get("itens") or []):
                        plano["duplicada"] = dn
                        break
                if plano["duplicada"] or len(lst) < 100:
                    break
            if plano["duplicada"]:
                dn = plano["duplicada"]
                sit = int(dn.get("situacao") or 0)
                quem = "importada / feita fora do Bling (ex.: o ML)" if sit == 7 else "feita no Bling"
                ponto(False, "Nota de devolução já existe",
                      f"NF {str(dn.get('numero') or '').lstrip('0')}/{dn.get('serie')} de {str(dn.get('dataEmissao') or '')[:10]} "
                      f"({SIT_NF.get(sit, sit)}, {quem}): não precisa gerar, só ligar à devolução")
            else:
                ponto(True, "Nota duplicada", "nenhuma nota de devolução desse cliente com esse produto no Bling")
        except Exception as e:
            ponto(None, "Nota duplicada", f"não consegui checar: {_erro_exc(e, 'procurar notas de entrada')[1]}")

    obs = (f"Devolução referente à NF-e {venda.get('numero')} série {venda.get('serie')}, chave {venda.get('chaveAcesso')}. "
           f"Venda {r['numero_venda']}. Devolução WMS #{r['id']}.")
    it = {"codigo": item.get("codigo"), "descricao": item.get("descricao"), "unidade": item.get("unidade") or "UN",
          "quantidade": qtd, "valor": item.get("valor"), "tipo": "P", "classificacaoFiscal": item.get("classificacaoFiscal"),
          "origem": item.get("origem", 0), "gtin": item.get("gtin") or "", "cfop": cfop}
    base = {"tipo": 0, "dataOperacao": time.strftime("%Y-%m-%d %H:%M:%S"), "contato": cont,
            "naturezaOperacao": {"id": nat}, "loja": {"id": (venda.get("loja") or {}).get("id") or 0}, "itens": [it],
            "finalidade": 4, "observacoes": obs}
    if (venda.get("intermediador") or {}).get("cnpj"):
        base["intermediador"] = venda["intermediador"]
    plano["payloads"] = [
        {**base, "documentoReferenciado": {"modelo": "55", "data": str(venda.get("dataEmissao") or "")[:10],
                                           "numero": str(venda.get("numero") or ""), "serie": str(venda.get("serie") or ""),
                                           "chaveAcesso": venda.get("chaveAcesso")}},
        base,   # se o Bling recusar o campo da referência: a chave fica nas observações (confira antes de enviar)
    ]
    return plano


def _conferir_criada(det, plano, com_ref=False):
    """Depois de criar: o que o Bling realmente gravou na nota. Tudo True = pode ir para a SEFAZ sozinha."""
    c = det.get("contato") or {}
    ref = det.get("documentoReferenciado") or det.get("notasReferenciadas") or det.get("documentosReferenciados")
    itens = det.get("itens") or []
    return [
        {"ok": True if (ref or com_ref) else None, "item": "Chave da NF de venda referenciada",
         "detalhe": "gravada (o Bling aceitou a referência)" if (ref or com_ref) else "o Bling não aceitou a referência: confira na aba de referências"},
        {"ok": True if c.get("email") else None, "item": "E-mail na nota", "detalhe": c.get("email") or "vazio"},
        {"ok": True if c.get("telefone") else None, "item": "Telefone na nota", "detalhe": c.get("telefone") or "vazio"},
        {"ok": True if itens else False, "item": "Item / CFOP na nota", "detalhe": ", ".join(f"{i.get('codigo')} x{i.get('quantidade')} CFOP {i.get('cfop')}" for i in itens) or "sem itens"},
        {"ok": True, "item": "Natureza", "detalhe": plano.get("natureza") or str((det.get("naturezaOperacao") or {}).get("id") or "-")},
        {"ok": True, "item": "ICMS na nota", "detalhe": _icms_txt(itens)},
    ]


def _icms_txt(itens):
    tot, partes = 0.0, []
    for i in itens:
        ic = (i.get("impostos") or {}).get("icms") or {}
        v = float(ic.get("valor") or 0)
        tot += v
        partes.append(f"{i.get('codigo')}: alíquota {ic.get('aliquota', '-')}% = R$ {v:.2f}".replace(".", ","))
    return (f"total R$ {tot:.2f}".replace(".", ",") + (f" ({'; '.join(partes)})" if partes else "")
            + (" — sem ICMS" if not tot else " — crédito do ICMS da venda (confira com o contador)"))


# ── andamento (roda em segundo plano: Bling + SEFAZ podem demorar mais que o limite do servidor) ──
ETAPAS_RODANDO = ("montando", "criando", "estoque", "conferindo", "sefaz")
ETAPA_TXT = {"montando": "Conferindo venda, cliente e nota no Bling", "criando": "Criando a nota no Bling",
             "estoque": "Lançando o estoque", "conferindo": "Lendo a nota criada no Bling",
             "sefaz": "Enviando para a SEFAZ", "ok": "Concluído", "erro": "Parou com erro"}
ENVIAR_SEFAZ_AUTO = lambda: os.environ.get("NF_DEV_ENVIAR_AUTO", "1") != "0"   # chave: NF_DEV_ENVIAR_AUTO=0 desliga


def _etapa(did, etapa, **campos):
    sets, vals = ["nf_dev_etapa=?", "nf_dev_etapa_em=?"], [etapa, int(time.time())]
    for k, v in campos.items():
        sets.append(f"{k}=?")
        vals.append(v)
    with db_session() as conn:
        conn.execute(f"UPDATE devolucoes SET {', '.join(sets)} WHERE id=?", (*vals, did))


def _falhou(did, origem, msg, situacao=None, **extra):
    campos = {"nf_dev_situacao": situacao} if situacao else {}
    _etapa(did, "erro", nf_dev_msg=msg[:500], nf_dev_erro=json.dumps({"error": msg, "origem": origem, **extra}, ensure_ascii=False)[:6000], **campos)
    print(f"[NF-DEV] devolução {did} PAROU ({origem}): {msg}", flush=True)


def _rodando(r):
    return (r["nf_dev_etapa"] in ETAPAS_RODANDO) and (time.time() - float(r["nf_dev_etapa_em"] or 0) < 240)


def _enviar_sefaz(b, did, nid, quem):
    """Transmite pelo Bling e espera a resposta da SEFAZ. -> (situação, mensagem, ok)"""
    _etapa(did, "sefaz")
    st, txt, js = _bling_uma_vez(b, "POST", f"/nfe/{nid}/enviar")
    print(f"[NF-DEV] devolução {did}: enviar SEFAZ HTTP {st} {txt[:400]}", flush=True)
    _nf_log(did, "enviar para a SEFAZ", st in (200, 201, 202, 204), "sefaz", st,
            "Bling aceitou o envio" if st in (200, 201, 202, 204) else (_msg_bling(txt) or f"HTTP {st}"), nid, txt, por=quem)
    sit = 0
    for _ in range(12):
        try:
            sit = int((b._get_json(f"/nfe/{nid}").get("data") or {}).get("situacao") or 0)
        except Exception as e:
            print(f"[NF-DEV] devolução {did}: lendo situação: {e}", flush=True)
        if sit in (2, 4, 5, 6, 7, 9):
            break
        time.sleep(3)
    nome = SIT_NF.get(sit, f"situação {sit}")
    if sit in (5, 6, 7):
        msg = f"AUTORIZADA pela SEFAZ ({nome})."
        ok = True
    elif sit in (4, 9):
        msg = f"{nome.upper()} pela SEFAZ: {_msg_bling(txt) if st not in (200, 201, 202, 204) else 'veja o motivo na nota, no Bling'}"
        ok = False
    elif st not in (200, 201, 202, 204):
        msg = f"O envio foi recusado: {_msg_bling(txt) or f'HTTP {st}'}"
        ok = False
    else:
        msg = f"Enviada; a SEFAZ ainda não respondeu ({nome}). Confira no Bling em alguns minutos."
        ok = None
    _nf_log(did, "resposta da SEFAZ", bool(ok), "sefaz", st, msg, nid, por=quem)
    return nome, msg, ok, (st, txt)


def _job_nf_dev(did, deposito, quem):
    """Gera a nota de devolução de ponta a ponta. Cada passo fica no histórico; qualquer erro para e mostra a origem."""
    from services.bling_service import BlingService
    try:
        with db_session() as conn:
            r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        try:
            p = _plano_nf_dev(r, deposito)
        except Exception as e:
            import traceback
            print(f"[NF-DEV] plano {did}: {traceback.format_exc()}", flush=True)
            org, msg = _erro_exc(e, "montar a nota")
            _nf_log(did, "montar", False, org, None, msg, por=quem)
            return _falhou(did, org, msg)
        if p["duplicada"]:
            dn = p["duplicada"]
            num = str(dn.get("numero") or "").lstrip("0")
            sitd = SIT_NF.get(int(dn.get("situacao") or 0), "")
            msg = f"Ligada à NF {num}/{dn.get('serie')} que já existia no Bling ({sitd}). O WMS não criou outra nem mexeu no estoque."
            _etapa(did, "ok", nf_dev_id=str(dn.get("id")), nf_dev_numero=num, nf_dev_msg=msg, nf_dev_em=int(time.time()),
                   nf_dev_por=None, nf_dev_situacao=sitd, nf_dev_erro=None,
                   nf_dev_conf=json.dumps(p["pontos"], ensure_ascii=False)[:6000])
            _nf_log(did, "ligar nota que já existia", True, "bling", 200, msg, dn.get("id"), por=quem)
            return
        if p["bloqueio"]:
            return _falhou(did, p.get("bloqueio_origem") or "conferencia", p["bloqueio"], pontos=p["pontos"])
        if p["dep"] not in NOME_DEP or not p["dep_id"]:
            return _falhou(did, "conferencia", "Escolha o depósito: Local (volta para venda) ou Defeitos / Garantia.", escolher=True, pontos=p["pontos"])

        b = BlingService()
        _etapa(did, "criando")
        criado, ult, com_ref = None, None, False
        for k, payload in enumerate(p["payloads"]):
            st, txt, js = _bling_uma_vez(b, "POST", "/nfe", payload)
            print(f"[NF-DEV] devolução {did} tentativa {k + 1}: HTTP {st} {txt[:400]}", flush=True)
            if st in (200, 201):
                criado = ((js or {}).get("data") or {}).get("id")
                com_ref = k == 0
                _nf_log(did, "criar nota", bool(criado), "bling", st, f"o Bling criou a nota (id {criado})" if criado else "o Bling respondeu sem id",
                        criado, txt, payload, por=quem)
                break
            ult = (st, txt)
            _nf_log(did, f"criar nota (tentativa {k + 1})", False, "bling", st, _msg_bling(txt) or f"sem resposta (HTTP {st})", None, txt, payload, por=quem)
            if st not in (400, 422):   # timeout/5xx: pode ter criado; não tenta de novo (evita nota dupla)
                break
        if not criado:
            st, txt = ult or (0, "")
            if st in (400, 422):
                msg = f"O Bling recusou a nota: {_msg_bling(txt)}"
            elif st == 0:
                msg = f"O Bling não respondeu ({txt[:200]}). Confira no Bling se a nota foi criada antes de tentar de novo."
            else:
                msg = f"O Bling deu erro (HTTP {st}): {_msg_bling(txt)}. Confira no Bling se a nota foi criada antes de tentar de novo."
            return _falhou(did, "bling", msg, http=st, campos=_campos_bling(txt), resposta=txt[:1500], pontos=p["pontos"])
        # grava o id JÁ: se algo falhar daqui pra frente, a devolução não perde a nota
        _etapa(did, "estoque", nf_dev_id=str(criado), nf_dev_por=quem, nf_dev_em=int(time.time()), nf_dev_situacao="Pendente",
               nf_dev_msg="nota criada no Bling; lançando estoque…")

        est_st, est_txt, _ = _bling_uma_vez(b, "POST", f"/nfe/{criado}/lancar-estoque/{p['dep_id']}")
        est_ok = est_st in (200, 201, 204)
        print(f"[NF-DEV] devolução {did}: lancar-estoque {p['dep_id']} HTTP {est_st} {est_txt[:300]}", flush=True)
        _nf_log(did, f"lançar estoque em {NOME_DEP[p['dep']]}", est_ok, "bling", est_st,
                "estoque lançado" if est_ok else (_msg_bling(est_txt) or f"HTTP {est_st}"), criado, est_txt, por=quem)

        _etapa(did, "conferindo", nf_dev_deposito=p["dep"] if est_ok else None)
        try:
            det = b._get_json(f"/nfe/{criado}").get("data") or {}
        except Exception as e:
            det = {"id": criado}
            _nf_log(did, "ler a nota criada", False, "bling", None, _erro_exc(e, "ler a nota criada")[1], criado, por=quem)
        conf = _conferir_criada(det, p, com_ref)
        conf.append({"ok": est_ok, "item": "Estoque", "detalhe": f"lançado em {NOME_DEP[p['dep']]}" if est_ok
                     else f"NÃO lançado ({_msg_bling(est_txt) or est_st}): lance no Bling"})
        pend = [c["item"] for c in conf if c["ok"] is not True]
        numero = str(det.get("numero") or "").lstrip("0")
        _nf_log(did, "conferir a nota no Bling", not pend, "bling", 200, "; ".join(f"{c['item']}: {c['detalhe']}" for c in conf),
                criado, json.dumps(det, ensure_ascii=False)[:4000], por=quem)
        base = f"NF {numero} criada no Bling. " + (f"Estoque lançado em {NOME_DEP[p['dep']]}. " if est_ok else "ATENÇÃO: estoque NÃO lançado. ")

        if pend or not ENVIAR_SEFAZ_AUTO():
            motivo = f"confira: {', '.join(pend)}" if pend else "envio automático desligado"
            _etapa(did, "ok", nf_dev_numero=numero, nf_dev_situacao="Pendente", nf_dev_erro=None,
                   nf_dev_conf=json.dumps(conf, ensure_ascii=False)[:6000],
                   nf_dev_msg=(base + f"NÃO enviei para a SEFAZ ({motivo}). Depois de conferir, clique em 'Enviar para a SEFAZ'.")[:500])
            return
        _etapa(did, "sefaz", nf_dev_numero=numero, nf_dev_conf=json.dumps(conf, ensure_ascii=False)[:6000])
        nome, msg_s, ok, (st, txt) = _enviar_sefaz(b, did, criado, quem)
        if ok is False:
            return _falhou(did, "sefaz", f"{base}{msg_s}", situacao=nome, http=st, campos=_campos_bling(txt), resposta=txt[:1500])
        _etapa(did, "ok", nf_dev_situacao=nome, nf_dev_erro=None, nf_dev_msg=(base + msg_s)[:500])
    except Exception as e:
        import traceback
        print(f"[NF-DEV] devolução {did}: {traceback.format_exc()}", flush=True)
        org, msg = _erro_exc(e, "gerar a nota")
        _nf_log(did, "erro", False, org, None, msg, por=quem)
        _falhou(did, org, msg)


def _rodar(alvo, *args):
    import threading
    threading.Thread(target=alvo, args=args, daemon=True).start()


def _info_nf(r):
    with db_session() as conn:
        log = [dict(x) for x in conn.execute("SELECT id, acao, ok, origem, http, mensagem, nf_id, resposta, enviado, por, em "
                                             "FROM devolucao_nf_log WHERE devolucao_id=? ORDER BY id DESC LIMIT 40", (r["id"],)).fetchall()]
    def _js(x):
        try:
            return json.loads(x) if x else None
        except Exception:
            return None
    etapa = r["nf_dev_etapa"]
    if etapa in ETAPAS_RODANDO and not _rodando(r):
        etapa = "erro"   # parou no meio (servidor reiniciou?)
    return {"sugerido": _deposito_sugerido(r), "nf_dev_numero": r["nf_dev_numero"], "nf_dev_id": r["nf_dev_id"],
            "nf_dev_deposito": r["nf_dev_deposito"], "nf_dev_por": r["nf_dev_por"], "nf_dev_em": r["nf_dev_em"],
            "nf_dev_msg": r["nf_dev_msg"], "full": r["status"] == "armazem_ml", "historico": log,
            "etapa": etapa, "etapa_txt": ETAPA_TXT.get(etapa or "", ""), "rodando": _rodando(r),
            "situacao": r["nf_dev_situacao"], "erro": _js(r["nf_dev_erro"]) if etapa == "erro" else None,
            "conferencia": _js(r["nf_dev_conf"]), "envio_auto": ENVIAR_SEFAZ_AUTO()}


@devolucoes_routes.get("/devolucoes/<int:did>/nf-devolucao")
@login_required
def nf_dev_info(did):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r:
        return jsonify({"error": "Devolução não encontrada."}), 404
    return jsonify(_info_nf(r))


@devolucoes_routes.get("/devolucoes/<int:did>/nf-devolucao/previa")
@login_required
def nf_dev_previa(did):
    """TESTE: mostra tudo o que seria feito, sem criar nada."""
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r:
        return jsonify({"error": "Devolução não encontrada."}), 404
    try:
        p = _plano_nf_dev(r, request.args.get("deposito"))
    except Exception as e:
        import traceback
        print(f"[NF-DEV] prévia {did}: {traceback.format_exc()}", flush=True)
        return _erro(*_erro_exc(e, "testar"))
    dn = p["duplicada"]
    return jsonify({"pode_gerar": not p["bloqueio"] and p["dep"] in NOME_DEP, "bloqueio": p["bloqueio"], "pontos": p["pontos"],
                    "duplicada": {"numero": str(dn.get("numero") or "").lstrip("0"), "serie": dn.get("serie"),
                                  "situacao": SIT_NF.get(int(dn.get("situacao") or 0))} if dn else None,
                    "sugerido": p["sugerido"], "deposito": p["dep"], "venda_nf": p["venda_nf"], "envio_auto": ENVIAR_SEFAZ_AUTO(),
                    "payload": p["payloads"][0] if p["payloads"] else None})


@devolucoes_routes.post("/devolucoes/<int:did>/nf-devolucao")
@login_required
def nf_dev_gerar(did):
    """Começa a nota de devolução (em segundo plano). A tela acompanha por GET .../nf-devolucao."""
    garantir_tabelas()
    body = request.get_json(silent=True) or {}
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r:
        return jsonify({"error": "Devolução não encontrada."}), 404
    if r["nf_dev_id"] or r["nf_dev_numero"]:
        return _erro("conferencia", f"Essa devolução já tem nota de devolução (NF {r['nf_dev_numero'] or r['nf_dev_id']}).", 409)
    if r["status"] == "armazem_ml":
        return _erro("conferencia", "Devolução FULL: volta para o armazém do ML; o WMS não gera nota.", 409)
    if not r["bipado"] or not r["chegada"]:
        return _erro("conferencia", "Conclua a conferência na prancheta antes.", 409)
    if _rodando(r):
        return jsonify({"success": True, "andamento": True, "msg": "Já está gerando."}), 202
    quem = _quem()
    _etapa(did, "montando", nf_dev_erro=None, nf_dev_msg=None)
    _nf_log(did, "começar", True, "wms", None, f"pedido por {quem}" + (f" · depósito {body.get('deposito')}" if body.get("deposito") else ""), por=quem)
    _rodar(_job_nf_dev, did, body.get("deposito"), quem)
    return jsonify({"success": True, "andamento": True}), 202


def _job_enviar(did, quem):
    from services.bling_service import BlingService
    try:
        with db_session() as conn:
            r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        b = BlingService()
        nome, msg, ok, (st, txt) = _enviar_sefaz(b, did, r["nf_dev_id"], quem)
        if ok is False:
            return _falhou(did, "sefaz", f"NF {r['nf_dev_numero']}: {msg}", situacao=nome, http=st, campos=_campos_bling(txt), resposta=txt[:1500])
        _etapa(did, "ok", nf_dev_situacao=nome, nf_dev_erro=None, nf_dev_msg=f"NF {r['nf_dev_numero']}: {msg}"[:500])
    except Exception as e:
        import traceback
        print(f"[NF-DEV] enviar {did}: {traceback.format_exc()}", flush=True)
        _falhou(did, *_erro_exc(e, "enviar para a SEFAZ"))


@devolucoes_routes.post("/devolucoes/<int:did>/nf-devolucao/enviar")
@login_required
def nf_dev_enviar(did):
    """Envio manual para a SEFAZ (supervisor/admin), quando o automático não foi (algo para conferir ou chave desligada)."""
    if not eh_gestor():
        return _erro("conferencia", "Só supervisor ou admin envia para a SEFAZ por aqui.", 403)
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r or not r["nf_dev_id"]:
        return _erro("conferencia", "Essa devolução não tem nota de devolução.", 404)
    if not r["nf_dev_por"]:
        return _erro("conferencia", "Essa nota não foi criada pelo WMS: envie pelo Bling.", 409)
    if _rodando(r):
        return jsonify({"success": True, "andamento": True}), 202
    quem = _quem()
    _etapa(did, "sefaz", nf_dev_erro=None)
    _rodar(_job_enviar, did, quem)
    return jsonify({"success": True, "andamento": True}), 202


@devolucoes_routes.post("/devolucoes/<int:did>/nf-devolucao/desfazer")
@login_required
def nf_dev_desfazer(did):
    """Volta atrás (supervisor/admin): só com a nota ainda PENDENTE ou REJEITADA. Estorna o estoque e exclui a nota no Bling."""
    from services.bling_service import BlingService
    if not eh_gestor():
        return _erro("conferencia", "Só supervisor ou admin desfaz a nota de devolução.", 403)
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r or not r["nf_dev_id"]:
        return _erro("conferencia", "Essa devolução não tem nota de devolução.", 404)
    if _rodando(r):
        return _erro("conferencia", "A nota ainda está sendo gerada/enviada. Espere terminar.", 409)
    limpar = ("UPDATE devolucoes SET nf_dev_id=NULL, nf_dev_numero=NULL, nf_dev_deposito=NULL, nf_dev_por=NULL, nf_dev_em=NULL, "
              "nf_dev_situacao=NULL, nf_dev_erro=NULL, nf_dev_conf=NULL, nf_dev_etapa=NULL, nf_dev_msg=? WHERE id=?")
    if not r["nf_dev_por"]:          # nota que já existia no Bling: o WMS só tinha ligado; não mexe nela
        with db_session() as conn:
            conn.execute(limpar, (f"desvinculada por {_quem()} (a nota não foi criada pelo WMS: continua no Bling)", did))
        _nf_log(did, "desvincular", True, "wms", None, "desvinculada (a nota continua no Bling)")
        return jsonify({"success": True, "msg": "Desvinculei. Essa nota não foi criada pelo WMS, então continua no Bling."})
    b = BlingService()
    nid = r["nf_dev_id"]
    try:
        det = b._get_json(f"/nfe/{nid}").get("data") or {}
    except Exception as e:
        return _erro(*_erro_exc(e, "ler a nota"))
    sit = int(det.get("situacao") or 0)
    if sit not in (1, 4):
        return _erro("conferencia", f"A NF {r['nf_dev_numero']} está '{SIT_NF.get(sit, sit)}' no Bling: não dá para desfazer pelo WMS. "
                                    "Se foi autorizada, cancele no Bling (prazo da SEFAZ: 24 h).", 409)
    passos, st, txt = [], 0, ""
    if r["nf_dev_deposito"]:
        ok = False
        for met, cam in (("DELETE", f"/nfe/{nid}/lancar-estoque"), ("POST", f"/nfe/{nid}/estornar-estoque")):
            st, txt, _ = _bling_uma_vez(b, met, cam)
            print(f"[NF-DEV] desfazer {did}: {met} {cam} -> {st} {txt[:200]}", flush=True)
            if st in (200, 201, 204):
                ok = True
                break
        passos.append("estoque estornado" if ok else f"NÃO consegui estornar o estoque ({_msg_bling(txt) or st}): estorne no Bling")
        _nf_log(did, "estornar estoque", ok, "bling", st, passos[-1], nid, txt)
    apagou = False
    for params in (None, {"idsNotas[]": nid}):
        st, txt, _ = _bling_uma_vez(b, "DELETE", f"/nfe/{nid}" if params is None else "/nfe", params=params)
        print(f"[NF-DEV] desfazer {did}: DELETE nfe {params or nid} -> {st} {txt[:200]}", flush=True)
        if st in (200, 204):
            apagou = True
            break
    passos.append(f"NF {r['nf_dev_numero']} excluída do Bling" if apagou else f"NÃO consegui excluir a NF {r['nf_dev_numero']}: {_msg_bling(txt) or st}. Exclua no Bling")
    _nf_log(did, "excluir nota", apagou, "bling", st, passos[-1], nid, txt)
    msg = f"Desfeito por {_quem()}: " + "; ".join(passos) + "."
    with db_session() as conn:
        if apagou:
            conn.execute(limpar, (msg, did))
        else:
            conn.execute("UPDATE devolucoes SET nf_dev_msg=? WHERE id=?", (msg, did))
    if not apagou:
        return _erro("bling", msg, 422)
    return jsonify({"success": True, "msg": msg})


@devolucoes_routes.get("/devolucoes/<int:did>/preencher")
@login_required
def preencher(did):
    """Busca o que falta e grava: canal, empresa, NF de saída (do ML), taxa de devolução (frete de volta)."""
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r:
        return jsonify({"error": "Devolução não encontrada."}), 404
    novos, fontes, avisos = {}, {}, []
    eh_ml = bool(r["ml_claim_id"]) or str(r["numero_venda"] or "").startswith("2000")
    if not r["canal"]:
        novos["canal"] = "MERCADO LIVRE" if eh_ml else None
        fontes["canal"] = "veio de uma reclamação do Mercado Livre" if eh_ml else ""
    if not r["loja"] or r["loja"] in ("LHV Med",):
        novos["loja"] = os.environ.get("DEVOLUCAO_EMPRESA_PADRAO", "LHVMED")
        fontes["loja"] = "conta do Mercado Livre deste WMS"
    if eh_ml and not (r["nf_saida"] or "").strip() and r["numero_venda"]:
        try:
            from services.ml_service import MercadoLivreService
            ml = MercadoLivreService()
            rr = ml._request("GET", f"/users/{ml.user_id}/invoices/orders/{r['numero_venda']}")
            if rr is not None and rr.status_code == 200:
                j = rr.json()
                num = str(j.get("invoice_number") or "").strip()
                if num:
                    novos["nf_saida"] = num + (f"/{j.get('invoice_series')}" if j.get("invoice_series") else "")
                    fontes["nf_saida"] = "nota fiscal da venda no Mercado Livre"
        except Exception as e:
            print(f"[NF-SAIDA] ML invoices {r['numero_venda']}: {e}", flush=True)
        if not novos.get("nf_saida"):                # o ML não tem a nota: busca no Bling, onde ela é emitida
            try:
                nf, info = _nf_saida_bling(r["numero_venda"])
                if nf:
                    novos["nf_saida"], fontes["nf_saida"] = nf, info
                else:
                    avisos.append(info)
            except Exception as e:
                avisos.append(f"NF de saída: erro ao consultar o Bling ({str(e)[:150]}) — preencha à mão")
    if r["custo_devolucao"] is None:
        taxa = r["custo_ml"]
        if taxa is None and r["ml_claim_id"]:
            try:
                taxa, _, _ = _custo_frete_volta(lambda p, params=None: _ml_req("GET", p, params=params), r["ml_claim_id"])
            except Exception as e:
                avisos.append(f"Taxa de devolução: {e}")
        if taxa is not None:
            novos["custo_devolucao"] = float(taxa)
            fontes["custo_devolucao"] = "frete de volta cobrado pelo Mercado Livre"
    novos = {k: v for k, v in novos.items() if v is not None}
    if novos:
        sets = ",".join(f"{k}=?" for k in novos)
        with db_session() as conn:
            conn.execute(f"UPDATE devolucoes SET {sets}, atualizado_em=? WHERE id=?", tuple(novos.values()) + (int(time.time()), did))
    return jsonify({"preenchido": novos, "fontes": fontes, "avisos": avisos, "empresas": EMPRESAS_FORMS, "servicos": SERVICOS})


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
    # janela máxima que o ML permite (60 dias). NÃO usar criado_em: é a data em que o WMS importou, não a da devolução
    fim = datetime.now(timezone.utc)
    ini = fim - timedelta(days=59)
    seller = str(os.environ.get("ML_SELLER_ID", "")).strip()
    ops, ultimo_erro = [], None
    # Sem filtro de tipo na chamada (o ML usa MAIÚSCULAS e ignora/recusa variações): pega tudo da janela,
    # pagina pelo 'scroll' e filtra aqui. Caminho sem /marketplace (o com /marketplace dá 403 'Invalid caller.id').
    for base in ("/stock/fulfillment/operations/search", "/marketplace/stock/fulfillment/operations/search"):
        params = {"seller_id": seller, "inventory_id": inv, "limit": 1000,
                  "date_from": ini.strftime("%Y-%m-%d"), "date_to": fim.strftime("%Y-%m-%d")}
        todas, ok = [], False
        for _ in range(10):
            st, j = _ml_req("GET", base, params=params)
            if st != 200 or not isinstance(j, dict):
                ultimo_erro = f"ML {st}: {str(j)[:200]}"
                break
            ok = True
            todas += j.get("results") or []
            sc = (j.get("paging") or {}).get("scroll") or j.get("scroll")
            if not sc or not j.get("results"):
                break
            params = {"seller_id": seller, "inventory_id": inv, "scroll": sc}
        if ok:
            ultimo_erro = None
            ops = [op for op in todas if "RETURN" in str(op.get("type") or "").upper()
                   or str(op.get("type") or "").upper() == "SALE_DELIVERY_CANCELATION"]
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
    if not (out["situacao"] == "nao_chegou" and ultimo_erro):       # guarda o resultado pro painel do admin
        with db_session() as conn:
            conn.execute("UPDATE devolucoes SET full_situacao=?, full_info=?, full_em=? WHERE id=?",
                         (out["situacao"], json.dumps({"operacoes": saida[-3:], "inventory_id": inv}), int(time.time()), did))
    return jsonify(out)


@devolucoes_routes.post("/devolucoes/full-conferir-todas")
@login_required
def full_conferir_todas():
    """Admin: confere no FULL as devoluções do armazém do ML (as mais antigas sem conferir primeiro). Poucas por vez."""
    garantir_tabelas()
    n = min(int((request.get_json(silent=True) or {}).get("limite") or 8), 15)
    with db_session() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM devolucoes WHERE status='armazem_ml' AND numero_venda IS NOT NULL "
            "ORDER BY COALESCE(full_em,0) ASC, criado_em ASC LIMIT ?", (n,)).fetchall()]
        total = conn.execute("SELECT COUNT(*) AS n FROM devolucoes WHERE status='armazem_ml'").fetchone()["n"]
    feitos, erros = 0, []
    for did in ids:
        try:
            r = full_retorno.__wrapped__(did) if hasattr(full_retorno, "__wrapped__") else full_retorno(did)
            feitos += 1
        except Exception as e:
            erros.append(f"#{did}: {e}")
        time.sleep(0.6)
    return jsonify({"success": True, "conferidas": feitos, "total_armazem": total, "erros": erros})


# ───────────────────────────── PAINEL DO ADMIN: quanto voltou bem e quanto virou prejuízo ─────────────────────────────
FULL_PRAZO_DIAS = int(os.environ.get("DEVOLUCAO_FULL_PRAZO_DIAS", "20"))   # depois disso, "não entrou no FULL" vira prejuízo


def _classificar(r) -> tuple:
    """-> (grupo, fonte). grupo: sem_custo | com_custo | em_aberto"""
    v = r["status"]
    if v == "armazem_ml":
        fs, quando = r["full_situacao"], r["full_em"]
        dt = time.strftime("%d/%m %H:%M", time.localtime(quando)) if quando else ""
        if fs == "disponivel":
            return "sem_custo", f"Histórico do estoque FULL do ML: devolução do comprador entrou DISPONÍVEL (consultado {dt})"
        if fs == "bloqueado":
            return "com_custo", f"Histórico do estoque FULL do ML: entrou BLOQUEADA, não vendável (consultado {dt})"
        idade = (time.time() - (r["criado_em"] or time.time())) / 86400
        if fs == "nao_chegou" and idade > FULL_PRAZO_DIAS:
            return "com_custo", f"Histórico do estoque FULL do ML: sem entrada de devolução após {int(idade)} dias (consultado {dt})"
        if fs == "nao_chegou":
            return "em_aberto", f"Ainda não entrou no FULL ({int(idade)} dias; prazo {FULL_PRAZO_DIAS}) — consultado {dt}"
        return "em_aberto", "Vai para o armazém do ML: ainda não conferido no histórico do FULL"
    if v == "esperada":
        return "em_aberto", f"A caminho do galpão (envio de volta no ML: {r['ml_envio'] or '—'})"
    if r["volta_estoque"] is None:
        return "em_aberto", "Chegou no galpão, mas a conferência ainda não disse se volta ao estoque"
    if r["volta_estoque"] and not tem_problema(r):
        return "sem_custo", "Conferência no galpão (WMS): produto em ordem, volta ao estoque"
    motivo = r["chegada"] or ("avaria" if r["avaria"] else "faltou item" if r["faltou_item"] else "não volta ao estoque")
    return "com_custo", f"Conferência no galpão (WMS): {motivo}" + (f" · ML avisado: {r['ml_review']}" if r["ml_review"] else "")


import re as _re
_SEM_CUSTO_RX = _re.compile(r"n[aã]o (a|o) quero mais|chegou bem|boas condi[cç]|n[aã]o [eé] da cor|arrepend|desist|n[aã]o (gostou|quer|precis)|mudou de ideia|comprou errado|tamanho|n[aã]o atend.*expectativ|engano", _re.I)
_COM_CUSTO_RX = _re.compile(r"descumpre|caracter[ií]sticas do an[uú]ncio|defeit|danific|quebr|avari|diferente|incomplet|falt|n[aã]o funciona|usad|vencid|errad", _re.I)


def _motivo_de(r) -> str:
    """Motivo escolhido na conferência; senão o motivo que o ML mandou (guardado nas observações pela importação)."""
    if (r["motivo"] or "").strip():
        return r["motivo"].strip()
    m = _re.search(r"Motivo ML:\s*(.+?)\.\s*(Envio de volta|$)", r["observacoes"] or "")
    if m and m.group(1).strip() not in ("—", ""):
        return _re.sub(r"\s*\([A-Z]{2,4}\d+\)\s*$", "", m.group(1)).strip()
    return "Não informado"


def _previsao(motivo: str) -> str:
    if _re.search(r"compr(ou|ei|a) errad|pedi errad", motivo or "", _re.I):
        return "sem_custo"
    if _COM_CUSTO_RX.search(motivo or ""):
        return "com_custo"
    if _SEM_CUSTO_RX.search(motivo or ""):
        return "sem_custo"
    return "indefinido"


@devolucoes_routes.get("/devolucoes/admin")
@login_required
def painel_admin():
    if not eh_gestor():
        return jsonify({"error": "Somente supervisor/admin."}), 403
    garantir_tabelas()
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM devolucoes ORDER BY criado_em DESC").fetchall()
    grupos = {k: {"valor": 0.0, "qtd": 0, "frete": 0.0, "itens": []} for k in ("sem_custo", "com_custo", "em_aberto")}
    previsto = {k: {"valor": 0.0, "qtd": 0} for k in ("sem_custo", "com_custo", "indefinido")}
    ranking = {}
    total, frete_total = 0.0, 0.0
    for r in rows:
        valor = float(r["valor"] or 0) * float(r["qtd"] or 1)
        frete = float(r["custo_devolucao"] if r["custo_devolucao"] is not None else (r["custo_ml"] or 0))
        g, fonte = _classificar(r)
        total += valor; frete_total += frete
        motivo = _motivo_de(r)
        rk = ranking.setdefault(motivo, {"motivo": motivo, "qtd": 0, "valor": 0.0, "frete": 0.0})
        rk["qtd"] += 1; rk["valor"] += valor; rk["frete"] += frete
        prev = None
        if g == "em_aberto":            # ainda não confirmado: previsão pelo motivo que o cliente deu
            prev = _previsao(motivo)
            previsto[prev]["valor"] += valor; previsto[prev]["qtd"] += 1
        G = grupos[g]
        G["valor"] += valor; G["qtd"] += 1; G["frete"] += frete
        G["itens"].append({"id": r["id"], "produto": r["produto"] or r["sku"], "sku": r["sku"], "venda": r["numero_venda"],
                           "cliente": r["cliente"], "valor": valor, "frete": frete, "status": r["status"], "fonte": fonte,
                           "criado_em": r["criado_em"], "motivo": motivo, "previsao": prev})
    return jsonify({"total": total, "qtd": len(rows), "frete_total": frete_total, "grupos": grupos, "previsto": previsto,
                    "ranking": sorted(ranking.values(), key=lambda x: (-x["qtd"], -x["valor"])),
                    "prazo_full_dias": FULL_PRAZO_DIAS,
                    "fontes": {
                        "valor": "Valor da venda × quantidade (vem do pedido no ML na importação, ou digitado na conferência)",
                        "frete": "Frete de volta cobrado do vendedor pelo ML (reclamação → devolução → envio, campo do recebedor); ou o custo digitado na conferência",
                        "galpao": "Devoluções que voltam ao galpão: resultado da conferência no WMS (como chegou, avaria, faltou item, pode voltar ao estoque)",
                        "full": "Devoluções FULL: histórico do estoque FULL do ML, operação 'devolução do comprador' (sale_return)",
                        "previsao": "Previsão (ainda não confirmada): pelo motivo que o cliente deu no ML — defeito, danificado, diferente, incompleto = provável custo; arrependimento, desistência = provável sem custo",
                        "motivo": "Motivo escolhido na conferência; se ainda não conferida, o motivo que o cliente informou no ML",
                    }})


@devolucoes_routes.get("/devolucoes/chegaram")
@login_required
def chegaram():
    """Devoluções que o ML diz que já foram ENTREGUES no galpão e ninguém conferiu ainda."""
    garantir_tabelas()
    with db_session() as conn:
        rows = conn.execute("SELECT id, produto, sku, cliente, numero_venda, atualizado_em FROM devolucoes "
                            "WHERE status='esperada' AND ml_envio='delivered' ORDER BY atualizado_em DESC LIMIT 50").fetchall()
    return jsonify({"n": len(rows), "itens": [dict(r) for r in rows]})


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
    canal = str(f.get("canal") or "").upper()
    channel = "Mercado Livre" if "MERCADO" in canal else "Shopee" if "SHOPEE" in canal else "Comercial" if "COMERCIAL" in canal else "Outros"
    emp = {"LHVMED": "LHV Med", "LHV MED": "LHV Med", "MEDICAL": "Medical D", "CONTA DA TICI": "Tici", "CONTA DA SIMONE": "Simone"}.get(str(f.get("loja") or "").upper(), f.get("loja"))
    obs = str(f.get("observacoes") or "")
    conf = obs.split("Conferência:", 1)[1].strip() if "Conferência:" in obs else ""
    texto = "; ".join(x for x in [problema, f"Observação da conferência: {conf}" if conf else "",
                                  f"Serviço: {f.get('servico')}" if f.get("servico") else "",
                                  f"Venda {f.get('numero_venda')}" if f.get("numero_venda") else "",
                                  f"Reclamação ML {f.get('ml_claim_id')}" if f.get("ml_claim_id") else "",
                                  f"Devolução WMS #{f['id']}"] if x)
    return {
        "origem": "wms-lhvmed", "tipo": "devolucao", "wms_id": f["id"],
        "type": "devolucao", "status": "aberto", "priority": "alta" if f.get("avaria") else "media",
        "name": f.get("cliente"), "numero_nf": f.get("nf_saida"), "invoice_number": f.get("nf_saida"),
        "purchase_origin": f.get("canal"), "channel": channel,
        "product": f"{f.get('sku')} - {f.get('produto') or ''}".strip(" -"),
        "problem_type": f"Devolução {channel}: {f.get('chegada') or f.get('motivo') or 'chegou com problema'}", "problem": texto, "empresa": emp,
        "criado_por": f.get("responsavel"), "devolucao": f,
    }


def enviar_ao_sac(r):
    """-> (status, msg, os_id). status: enviado | simulado | erro."""
    base = os.environ.get("SAC_API_URL", "").strip().rstrip("/")
    if not base:
        return "simulado", "SAC_API_URL não definido: nada foi enviado (modo simulado).", None
    path = os.environ.get("SAC_DEVOLUCAO_PATH", "/api/service-orders").strip()
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
        os_id = str(j.get("id") or j.get("os_id") or (j.get("data") or {}).get("id") or "") or None
        # fotos da conferência como anexos da O.S.
        enviadas = 0
        if os_id:
            try:
                with db_session() as conn:
                    fs = conn.execute("SELECT arquivo FROM devolucao_fotos WHERE devolucao_id=? ORDER BY id", (r["id"],)).fetchall()
                files = []
                for k, fr in enumerate(fs, 1):
                    cam = os.path.join(_pasta_fotos(), os.path.basename(fr["arquivo"]))
                    if os.path.exists(cam):
                        files.append(("files", (f"devolucao_{r['id']}_foto{k}.jpg", open(cam, "rb"), "image/jpeg")))
                if files:
                    ra = requests.post(f"{base}{path}/{os_id}/anexos", files=files, headers={k: v for k, v in h.items() if k != "Content-Type"}, timeout=30)
                    enviadas = len(files) if ra.status_code < 300 else 0
                    for _, (_, fh, _) in files:
                        fh.close()
            except Exception:
                pass
        return "enviado", f"O.S. {os_id} criada" + (f" com {enviadas} foto(s)" if enviadas else ""), os_id
    except Exception as e:  # noqa: BLE001
        return "erro", f"falha ao falar com o SAC: {e}"[:300], None


@devolucoes_routes.post("/devolucoes/<int:did>/sac")
@login_required
def sac(did):
    """Envia (ou reenvia, se deu erro) a devolução ao SAC. Já tem O.S.: não duplica."""
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        if r["sac_os_id"]:
            return jsonify({"success": True, "sac_status": "enviado", "msg": f"Já está no SAC: O.S. {r['sac_os_id']}", "os_id": r["sac_os_id"]})
        st, msg, osid = enviar_ao_sac(r)
        conn.execute("UPDATE devolucoes SET sac_status=?, sac_msg=?, sac_os_id=?, sac_enviado_em=? WHERE id=?",
                     ("enviado" if st == "enviado" else ("pendente" if st == "simulado" else "erro"),
                      msg, osid, int(time.time()) if st == "enviado" else None, did))
    if st == "erro":
        return jsonify({"error": msg, "sac_status": st}), 422
    return jsonify({"success": True, "sac_status": st, "msg": msg, "os_id": osid})


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
SYNC_LOCK = "/tmp/wms_devolucoes_sync.lock"
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
RECHECAR_CUSTO_S = 10 * 60         # status do envio de volta de devolução aberta: atualiza a cada 20 min (aviso de "chegou")


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
        "OR (status IN ('esperada','armazem_ml') AND custo_ml_em < ?)) "
        # prioridade: a caminho do NOSSO galpão sem revisão no ML (tem prazo) > resto; dentro disso, quem está há mais tempo sem checar
        "ORDER BY CASE WHEN status='esperada' AND ml_review IS NULL THEN 0 ELSE 1 END, COALESCE(custo_ml_em, 0), id LIMIT ?",
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
            if v and ((v["resultado"] in ("importada", "armazem_ml") and tem_linha)
                      or (v["resultado"] in ("sem_devolucao", "fechada_ok") or str(v["resultado"]).startswith("erro_"))
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
                    # reclamação fechada e a caixa já chegou. MAS se o ML ainda espera a REVISÃO do vendedor
                    # ("Para sua revisão" no painel do ML), ela continua pendente pra nós: importa.
                    cc, cl = ml_get(f"/post-purchase/v1/claims/{cid}")
                    if cc == 429:
                        parou_429 = True
                        continue
                    if not (cc == 200 and any(a in _acoes_seller(cl or {}) for a in ("return_review_ok", "return_review_fail"))):
                        resultado = "fechada_ok"
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
    # o ML limita MUITO as consultas de reclamação (429): no máximo 1 importação a cada 5 min, e nenhuma
    # enquanto o script devolucoes_sincronizar.py estiver rodando (ele cria SYNC_LOCK)
    if os.path.exists(SYNC_LOCK) and agora - os.path.getmtime(SYNC_LOCK) < 3600:
        return jsonify({"ok": True, "pulado": True, "motivo": "sincronização manual em andamento"})
    if agora - _ultima_importacao < 300:
        return jsonify({"ok": True, "pulado": True, "motivo": "importado há menos de 5 min"})
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
