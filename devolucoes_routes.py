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

from flask import Blueprint, Response, jsonify, request
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
STATUS = [("recebida", "Recebida"), ("aguardando_nf", "Aguardando NF de devolução"),
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
    return d


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
    with db_session() as conn:
        p = conn.execute("SELECT id, ml_id, cliente_nome FROM pedidos WHERE ml_id=? LIMIT 1", (q,)).fetchone()
        if not p:
            return jsonify({"encontrado": False})
        itens = conn.execute(
            "SELECT pr.sku AS sku, pr.nome AS nome, pp.quantidade AS qtd FROM pedido_produtos pp "
            "JOIN produtos pr ON pr.id = pp.produto_id WHERE pp.pedido_id=?", (p["id"],)).fetchall()
    return jsonify({"encontrado": True, "numero_venda": p["ml_id"], "cliente": p["cliente_nome"],
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
    return jsonify({"itens": [_fmt(r) for r in rows]})


@devolucoes_routes.get("/devolucoes/resumo")
@login_required
def resumo():
    garantir_tabelas()
    with db_session() as conn:
        tot = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(valor*qtd),0) AS v FROM devolucoes").fetchone()
        por = conn.execute("SELECT status, COUNT(*) AS n FROM devolucoes GROUP BY status").fetchall()
        sac = conn.execute("SELECT COUNT(*) AS n FROM devolucoes WHERE sac_status<>'enviado'").fetchone()["n"]
        volta = conn.execute("SELECT COUNT(*) AS n FROM devolucoes WHERE volta_estoque=1").fetchone()["n"]
    return jsonify({"total": tot["n"], "valor": float(tot["v"] or 0), "por_status": {r["status"]: r["n"] for r in por},
                    "sac_pendentes": sac, "voltam_estoque": volta})


@devolucoes_routes.get("/devolucoes/<int:did>")
@login_required
def detalhe(did):
    garantir_tabelas()
    with db_session() as conn:
        r = conn.execute("SELECT * FROM devolucoes WHERE id=?", (did,)).fetchone()
    if not r:
        return jsonify({"error": "Devolução não encontrada."}), 404
    return jsonify(_fmt(r))


@devolucoes_routes.patch("/devolucoes/<int:did>")
@login_required
def editar(did):
    d = request.get_json(silent=True) or {}
    garantir_tabelas()
    novos = _aplicar(d)
    novos.pop("sku_key", None) if "sku" not in d else None
    if not novos:
        return jsonify({"error": "Nada para alterar."}), 400
    novos["atualizado_em"] = int(time.time())
    sets = ",".join(f"{c}=?" for c in novos)
    with db_session() as conn:
        r = conn.execute("SELECT id, sac_status FROM devolucoes WHERE id=?", (did,)).fetchone()
        if not r:
            return jsonify({"error": "Devolução não encontrada."}), 404
        # alterou depois de enviada ao SAC: volta a "pendente" para reenviar a versão certa
        if r["sac_status"] == "enviado":
            sets += ",sac_status='pendente'"
        conn.execute(f"UPDATE devolucoes SET {sets} WHERE id=?", tuple(novos.values()) + (did,))
    return jsonify({"success": True})


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
            "valor", "serie_lote", "nf_saida", "teve_reclamacao", "num_reclamacao", "motivo", "alegacao_defeito",
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
