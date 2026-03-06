import os
import re
import time
import traceback
import unicodedata
from datetime import datetime
from services.estrutura_service import EstruturaService
from services.serial_store import SerialStore

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, send_from_directory, abort, send_file

from db.database import get_db
from services.bling_service import BlingService
from services.ml_service import MercadoLivreService
from services.document_service import DocumentService

# (se você usa esses serviços em algum lugar do projeto, pode manter)
# from services.zpl_converter import ZPLConverterService
# from services.pdf_printer_service import PDFPrinterService

main_routes = Blueprint("main", __name__)

# ============================================================
# Helpers
# ============================================================

def _is_fetch(req):
    return req.headers.get("X-Requested-With") == "fetch"


def _tipo_envio_label(logistic_type: str) -> str:
    lt = (logistic_type or "").lower()
    if lt == "self_service":
        return "FLEX"
    if lt in ("cross_docking", "drop_off", "xd_drop_off"):
        return "COLETA/AGÊNCIA"
    return lt.upper() if lt else "PADRÃO"


def _norm_txt(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def _group_key(nome: str) -> str:
    """Se você quiser agrupar itens parecidos no front depois."""
    s = _norm_txt(nome)
    s = re.sub(r"\b(tamanho|tam|nº|n\.)\b", " ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    s = re.sub(r"[|:/\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split()
    return " ".join(tokens[:4]) if tokens else s


def _attrs_text_ml(order_item: dict) -> str:
    """Atributos/variações curtos (TAMANHO etc)."""
    item = (order_item or {}).get("item") or {}
    attrs = []
    for src in (
        order_item.get("variation_attributes"),
        item.get("variation_attributes"),
        order_item.get("attributes"),
        item.get("attributes"),
    ):
        if isinstance(src, list):
            for a in src:
                if not isinstance(a, dict):
                    continue
                name = (a.get("name") or a.get("id") or "").strip()
                val = (a.get("value_name") or a.get("value_id") or "").strip()
                if name and val:
                    attrs.append(f"{name}: {val}")

    uniq = []
    for x in attrs:
        if x not in uniq:
            uniq.append(x)

    return " | " + " / ".join(uniq[:3]) if uniq else ""


def _extrair_sku_ml(order_item: dict) -> str | None:
    """
    Prioridade:
      1) order_item["seller_sku"]
      2) order_item["item"]["seller_sku"]
      3) attributes/variation_attributes com id SELLER_SKU / SELLER_CUSTOM_FIELD / SKU
    """
    if not isinstance(order_item, dict):
        return None

    v = order_item.get("seller_sku")
    if v:
        return str(v).strip()

    info = order_item.get("item") or {}
    v = info.get("seller_sku")
    if v:
        return str(v).strip()

    def pick_from_attrs(attrs):
        if not isinstance(attrs, list):
            return None
        for a in attrs:
            if not isinstance(a, dict):
                continue
            aid = (a.get("id") or "").upper()
            if aid in ("SELLER_SKU", "SELLER_CUSTOM_FIELD", "SKU"):
                vv = a.get("value_name") or a.get("value_id") or a.get("value") or a.get("name")
                if vv:
                    return str(vv).strip()
        return None

    v = pick_from_attrs(info.get("attributes")) or pick_from_attrs(info.get("variation_attributes"))
    if v:
        return v

    v = pick_from_attrs(order_item.get("variation_attributes")) or pick_from_attrs(order_item.get("attributes"))
    return v


def _sku_final_para_db(order_item: dict) -> str:
    """SKU final salvo no DB: seller_sku se existir, senão item.id."""
    info = order_item.get("item") or {}
    sku_ml = _extrair_sku_ml(order_item)
    if sku_ml:
        return sku_ml
    return str(info.get("id") or "SEM_SKU").strip()


def _pedido_dt(criado_em):
    if isinstance(criado_em, str):
        try:
            return datetime.strptime(criado_em.split(".")[0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.now()
    return criado_em or datetime.now()


def _detect_unique_pp(cur) -> bool:
    """
    Detecta se existe UNIQUE(pedido_id, produto_id) em pedido_produtos
    (pra usar ON CONFLICT e evitar SELECT no upsert).
    """
    try:
        idxs = cur.execute("PRAGMA index_list('pedido_produtos')").fetchall()
        for idx in idxs:
            # sqlite PRAGMA index_list: [seq, name, unique, origin, partial]
            idx_name = idx[1]
            is_unique = int(idx[2] or 0) == 1
            if not is_unique:
                continue
            cols = cur.execute(f"PRAGMA index_info('{idx_name}')").fetchall()
            col_names = [c[2] for c in cols]  # [seqno, cid, name]
            if col_names == ["pedido_id", "produto_id"] or col_names == ["produto_id", "pedido_id"]:
                return True
    except Exception:
        pass
    return False


def _upsert_pedido_produto(cur, has_unique_composto: bool, pedido_id: int, produto_id: int,
                          quantidade: int, parent_sku=None, parent_nome=None):
    """
    Upsert em pedido_produtos.
    - Se tiver UNIQUE(pedido_id, produto_id): usa INSERT..ON CONFLICT (rápido).
    - Se não tiver: cai em upsert manual (SELECT + UPDATE/INSERT).
    """
    q = int(quantidade or 0)

    if has_unique_composto:
        cur.execute("""
            INSERT INTO pedido_produtos (
                pedido_id, produto_id, quantidade, quantidade_bipada, parent_sku, parent_nome
            ) VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(pedido_id, produto_id) DO UPDATE SET
                quantidade = excluded.quantidade,
                parent_sku = COALESCE(excluded.parent_sku, pedido_produtos.parent_sku),
                parent_nome = COALESCE(excluded.parent_nome, pedido_produtos.parent_nome)
        """, (pedido_id, produto_id, q, parent_sku, parent_nome))
        return

    row = cur.execute(
        "SELECT id FROM pedido_produtos WHERE pedido_id=? AND produto_id=?",
        (pedido_id, produto_id)
    ).fetchone()

    if row:
        cur.execute("""
            UPDATE pedido_produtos
               SET quantidade = ?,
                   parent_sku = COALESCE(?, parent_sku),
                   parent_nome = COALESCE(?, parent_nome)
             WHERE pedido_id = ?
               AND produto_id = ?
        """, (q, parent_sku, parent_nome, pedido_id, produto_id))
    else:
        cur.execute("""
            INSERT INTO pedido_produtos (
                pedido_id, produto_id, quantidade, quantidade_bipada, parent_sku, parent_nome
            ) VALUES (?, ?, ?, 0, ?, ?)
        """, (pedido_id, produto_id, q, parent_sku, parent_nome))


# ============================================================
# Carregar pedidos + itens com 1 query (RÁPIDO)
# ============================================================

def _load_pedidos(status: str):
    db = get_db()
    rows = db.execute("""
        SELECT
        p.id as pedido_id, p.ml_id, p.cliente_nome, p.status, p.logistic_type,
        p.criado_em, p.ml_ship_substatus, p.ml_task,
        pr.sku as sku, pr.nome as nome, pr.estoque_bling as estoque_bling,
        pp.quantidade as quantidade, pp.quantidade_bipada as quantidade_bipada,
        pp.parent_sku as parent_sku, pp.parent_nome as parent_nome
        FROM pedidos p
        LEFT JOIN pedido_produtos pp ON pp.pedido_id = p.id
        LEFT JOIN produtos pr ON pr.id = pp.produto_id
        WHERE p.status = ?
        ORDER BY p.criado_em DESC, p.id DESC
    """, (status,)).fetchall()

    pedidos = {}
    for r in rows:
        pid = r["pedido_id"]

        if pid not in pedidos:
            pedidos[pid] = {
                "id": pid,
                "ml_id": r["ml_id"],
                "cliente_nome": r["cliente_nome"],
                "status": r["status"],
                "logistic_type": r["logistic_type"],
                "criado_em": _pedido_dt(r["criado_em"]),
                "ml_ship_substatus": r["ml_ship_substatus"],
                "ml_task": r["ml_task"],
                "envio_label": _tipo_envio_label(r["logistic_type"]),
                "itens": [],
                "total_quantidade": 0,
                "total_bipado": 0,
            }

        # item pode ser None
        if r["sku"] is not None:
            it = {
                "sku": r["sku"],
                "nome": r["nome"] or "Produto",
                "estoque_bling": r["estoque_bling"],
                "quantidade": int(r["quantidade"] or 0),
                "quantidade_bipada": int(r["quantidade_bipada"] or 0),
                "parent_sku": r["parent_sku"],
                "parent_nome": r["parent_nome"],
            }
            # aliases pra template antigo não quebrar
            it["saldo"] = it["estoque_bling"]
            it["estoque"] = it["estoque_bling"]
            it["estoque_atual"] = it["estoque_bling"]
            it["bling_estoque"] = it["estoque_bling"]
            it["estoqueAtual"] = it["estoque_bling"]

            pedidos[pid]["itens"].append(it)
            pedidos[pid]["total_quantidade"] += it["quantidade"]
            pedidos[pid]["total_bipado"] += it["quantidade_bipada"]

    out = list(pedidos.values())
    db.close()
    return out


def _calc_stats(pedidos):
    total_itens = sum(int(p.get("total_quantidade") or 0) for p in pedidos)
    total_bip = sum(int(p.get("total_bipado") or 0) for p in pedidos)
    return {
        "pendentes": len(pedidos),
        "total_itens": total_itens,
        "total_bipado": total_bip,
        "progresso_geral_pct": int((total_bip / total_itens) * 100) if total_itens else 0
    }


# ============================================================
# TELA 1 - EXPEDIÇÃO
# ============================================================

@main_routes.route("/")
def index():
    pedidos = _load_pedidos("PENDENTE")
    # progresso individual (template usa bastante)
    for p in pedidos:
        tq = int(p.get("total_quantidade") or 0)
        tb = int(p.get("total_bipado") or 0)
        prog = int((tb / tq) * 100) if tq else 0
        p["progresso_pct"] = prog
        p["progresso_bar"] = f"width: {prog}%"

    for p in pedidos:
        if p["itens"]:
            p["sort_key"] = _group_key(p["itens"][0]["nome"])
        else:
            p["sort_key"] = "zzz"

    pedidos.sort(key=lambda x: x["sort_key"])

    stats = _calc_stats(pedidos)
    stats["concluidos"] = sum(1 for p in pedidos if int(p.get("progresso_pct") or 0) >= 100)
    return render_template("index.html", pedidos=pedidos, stats=stats)


@main_routes.route("/pedido/<ml_id>/json")
def pedido_json(ml_id):
    db = get_db()
    p = db.execute("SELECT id, ml_id FROM pedidos WHERE ml_id=? LIMIT 1", (str(ml_id),)).fetchone()
    if not p:
        db.close()
        return jsonify({"success": False}), 404

    itens = db.execute("""
      SELECT pr.sku, pr.nome, pr.estoque_bling,
             pp.quantidade, pp.quantidade_bipada,
             pp.parent_sku, pp.parent_nome
      FROM pedido_produtos pp
      JOIN produtos pr ON pr.id = pp.produto_id
      WHERE pp.pedido_id = ?
    """, (p["id"],)).fetchall()

    itens_list = [dict(i) for i in itens]
    tot = sum(int(i.get("quantidade") or 0) for i in itens_list)
    bip = sum(int(i.get("quantidade_bipada") or 0) for i in itens_list)
    db.close()

    return jsonify({
        "success": True,
        "ml_id": p["ml_id"],
        "total_quantidade": tot,
        "total_bipado": bip,
        "itens": itens_list
    })


# ============================================================
# SYNC (otimizado)
# ============================================================

@main_routes.route("/sincronizar", methods=["POST"])
def sincronizar():
    try:
        ml = MercadoLivreService()
        bling = BlingService()
        _estoque_cache: dict[str, int | None] = {}

        cards = ml.buscar_cards(tz_offset_hours=-3)

        flex = cards.get("flex_ready_to_print", []) or []
        coleta = cards.get("coleta_invoices_to_be_managed", []) or []
        results = flex + coleta

        if not results:
            msg = "Nenhum pedido (Imprimir etiqueta / Emitir NF-e) encontrado agora."
            if _is_fetch(request):
                return jsonify({"success": False, "message": msg}), 200
            flash("⚠️ " + msg, "warning")
            return redirect(url_for("main.index"))

        db = get_db()
        cur = db.cursor()

        # pega lock de escrita logo (evita “database is locked” no meio)
        cur.execute("BEGIN IMMEDIATE;")

        cols = cur.execute("PRAGMA table_info(pedidos)").fetchall()
        col_names = {c[1] for c in cols}
        has_ml_task = "ml_task" in col_names
        has_bling_pv = "bling_pedido_venda_id" in col_names

        count_pedidos = 0
        count_itens = 0

        produto_id_cache = {}  # sku -> produto_id
        pedido_id_cache = {}   # ml_id -> pedido_id

        for ped in results:
            ml_id = str(ped.get("id") or "").strip()
            if not ml_id:
                continue

            cliente_nome = (ped.get("buyer") or {}).get("nickname", "CLIENTE")

            shipping = ped.get("shipping") or {}
            logistic_type = shipping.get("logistic_type") or "Padrão"

            ml_shipping_id = shipping.get("ml_shipping_id") or shipping.get("id")
            ml_ship_status = shipping.get("ml_ship_status")
            ml_ship_substatus = shipping.get("ml_ship_substatus")
            ml_task = ped.get("ml_task")

            # zera PV (se existir coluna)
            pv_value = None

            # -----------------------------
            # UPSERT pedido
            # -----------------------------
            if has_ml_task:
                cur.execute("""
                    INSERT INTO pedidos (
                        ml_id, cliente_nome, logistic_type,
                        ml_shipping_id, ml_ship_status, ml_ship_substatus,
                        ml_task, status
                        {pv_col}
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDENTE'
                        {pv_val}
                    )
                    ON CONFLICT(ml_id) DO UPDATE SET
                        cliente_nome=excluded.cliente_nome,
                        logistic_type=excluded.logistic_type,
                        ml_shipping_id=excluded.ml_shipping_id,
                        ml_ship_status=excluded.ml_ship_status,
                        ml_ship_substatus=excluded.ml_ship_substatus,
                        ml_task=excluded.ml_task,
                        atualizado_em=CURRENT_TIMESTAMP
                        {pv_upd}
                """.format(
                    pv_col=", bling_pedido_venda_id" if has_bling_pv else "",
                    pv_val=", ?" if has_bling_pv else "",
                    pv_upd=", bling_pedido_venda_id=excluded.bling_pedido_venda_id" if has_bling_pv else ""
                ), tuple(
                    [ml_id, cliente_nome, logistic_type,
                     str(ml_shipping_id) if ml_shipping_id else None,
                     ml_ship_status, ml_ship_substatus, ml_task] + ([pv_value] if has_bling_pv else [])
                ))
            else:
                cur.execute("""
                    INSERT INTO pedidos (
                        ml_id, cliente_nome, logistic_type,
                        ml_shipping_id, ml_ship_status, ml_ship_substatus,
                        status
                        {pv_col}
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDENTE'
                        {pv_val}
                    )
                    ON CONFLICT(ml_id) DO UPDATE SET
                        cliente_nome=excluded.cliente_nome,
                        logistic_type=excluded.logistic_type,
                        ml_shipping_id=excluded.ml_shipping_id,
                        ml_ship_status=excluded.ml_ship_status,
                        ml_ship_substatus=excluded.ml_ship_substatus,
                        atualizado_em=CURRENT_TIMESTAMP
                        {pv_upd}
                """.format(
                    pv_col=", bling_pedido_venda_id" if has_bling_pv else "",
                    pv_val=", ?" if has_bling_pv else "",
                    pv_upd=", bling_pedido_venda_id=excluded.bling_pedido_venda_id" if has_bling_pv else ""
                ), tuple(
                    [ml_id, cliente_nome, logistic_type,
                     str(ml_shipping_id) if ml_shipping_id else None,
                     ml_ship_status, ml_ship_substatus] + ([pv_value] if has_bling_pv else [])
                ))

            # -----------------------------
            # resolve pedido_id (cache)
            # -----------------------------
            if ml_id in pedido_id_cache:
                pedido_id = pedido_id_cache[ml_id]
            else:
                row = cur.execute("SELECT id FROM pedidos WHERE ml_id=?", (ml_id,)).fetchone()
                if not row:
                    continue
                pedido_id = int(row[0])
                pedido_id_cache[ml_id] = pedido_id

            # -----------------------------
            # CARREGA ITENS DO PEDIDO NO ML
            # -----------------------------
            order = ml.obter_pedido(ml_id)  # <<< precisa existir no ml_service.py
            order_items = (order or {}).get("order_items") or []

            # transforma em lista padronizada
            produtos_expandidos = []
            for oi in order_items:
                info = oi.get("item") or {}
                nome = (info.get("title") or "Produto").strip()

                # inclui atributos (tamanho/cor) no nome (opcional, mas ajuda)
                nome = nome + _attrs_text_ml(oi)

                sku_db = _sku_final_para_db(oi)  # sua função: seller_sku ou item.id
                qtd = int(oi.get("quantity") or 1)

                if sku_db:

                    estrutura_service = EstruturaService()
                    estrutura = estrutura_service.obter_estrutura(str(sku_db).strip())

                    # se não for kit
                    if not estrutura:
                        produtos_expandidos.append({
                            "sku": str(sku_db).strip(),
                            "qtd": qtd,
                            "nome": nome,
                            "parent_sku": None,
                            "parent_nome": None,
                        })

                    # se for kit
                    else:
                        for comp in estrutura:

                            comp_sku = str(comp.get("sku")).strip()
                            comp_qtd = int(comp.get("quantidade", 1)) * qtd

                            produtos_expandidos.append({
                                "sku": comp_sku,
                                "qtd": comp_qtd,
                                "nome": comp.get("nome") or nome,
                                "parent_sku": sku_db,
                                "parent_nome": nome
                            })

            # -----------------------------
            # SALVA ITENS NO DB
            # -----------------------------
            for prod in produtos_expandidos:
                sku = str(prod.get("sku") or "").strip()
                qtd = int(prod.get("qtd") or 1)
                nome = str(prod.get("nome") or "Produto")

                parent_sku = prod.get("parent_sku")
                parent_nome = prod.get("parent_nome")

                if not sku:
                    continue

                # produto_id cache
                produto_id = produto_id_cache.get(sku)
                if not produto_id:
                    cur.execute("""
                        INSERT INTO produtos (sku, nome)
                        VALUES (?, ?)
                        ON CONFLICT(sku) DO UPDATE SET nome=excluded.nome
                    """, (sku, nome))
                    prow = cur.execute("SELECT id FROM produtos WHERE sku=?", (sku,)).fetchone()
                    if not prow:
                        continue
                    produto_id = int(prow[0])
                    produto_id_cache[sku] = produto_id

                # estoque Bling (cache por SKU)
                try:
                    if sku not in _estoque_cache:
                        _estoque_cache[sku] = None
                        try:
                            _estoque_cache[sku] = int(bling.obter_estoque(sku))
                        except Exception:
                            _estoque_cache[sku] = None

                    if _estoque_cache.get(sku) is not None:
                        cur.execute("UPDATE produtos SET estoque_bling=? WHERE sku=?", (int(_estoque_cache[sku]), sku))
                except Exception:
                    pass

                # vínculo pedido_produtos (upsert simples)
                pp = cur.execute("""
                    SELECT id FROM pedido_produtos
                    WHERE pedido_id=? AND produto_id=?
                """, (pedido_id, produto_id)).fetchone()

                if pp:
                    cur.execute("""
                        UPDATE pedido_produtos
                        SET quantidade=?,
                            parent_sku=COALESCE(?, parent_sku),
                            parent_nome=COALESCE(?, parent_nome)
                        WHERE pedido_id=? AND produto_id=?
                    """, (qtd, parent_sku, parent_nome, pedido_id, produto_id))
                else:
                    cur.execute("""
                        INSERT INTO pedido_produtos (
                            pedido_id, produto_id, quantidade, quantidade_bipada,
                            parent_sku, parent_nome
                        ) VALUES (?, ?, ?, 0, ?, ?)
                    """, (pedido_id, produto_id, qtd, parent_sku, parent_nome))

                count_itens += 1

            count_pedidos += 1

        db.commit()
        db.close()

        msg = f"Sync concluída! {count_pedidos} pedidos / {count_itens} itens."
        if _is_fetch(request):
            return jsonify({"success": True, "message": msg, "pedidos": count_pedidos, "itens": count_itens}), 200

        flash("✅ " + msg, "success")
        return redirect(url_for("main.index"))

    except Exception as e:
        traceback.print_exc()
        err = str(e)
        if _is_fetch(request):
            return jsonify({"success": False, "message": err}), 500
        flash(f"❌ Erro ao sincronizar: {err}", "danger")
        return redirect(url_for("main.index"))


# ============================================================
# BIPAR (tela 1 -> separação)
# ============================================================

@main_routes.route("/verificar_e_bipar", methods=["POST"])
def verificar_e_bipar():
    data = request.get_json(silent=True) or {}
    sku = (data.get("sku") or "").strip()
    ml_id = (data.get("ml_id") or "").strip()   # <-- NOVO: obrigar contexto do pedido

    if not sku:
        return jsonify({"success": False, "error": "SKU vazio"}), 400
    if not ml_id:
        return jsonify({"success": False, "error": "ml_id obrigatório (pedido selecionado)"}), 400

    db = get_db()
    cur = db.cursor()

    row = cur.execute("""
        SELECT
            pp.id as pp_id,
            p.id as pedido_id,
            p.ml_id,
            pp.quantidade,
            pp.quantidade_bipada
        FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        JOIN pedidos p ON p.id = pp.pedido_id
        WHERE UPPER(pr.sku) = UPPER(?)
          AND p.ml_id = ?
          AND p.status = 'PENDENTE'
          AND pp.quantidade_bipada < pp.quantidade
        LIMIT 1
    """, (sku, ml_id)).fetchone()

    if not row:
        db.close()
        return jsonify({"success": False, "error": "SKU não encontrado nesse pedido ou já bipado"}), 404

    # ... resto do teu código igual (update quantidade_bipada, totals, etc)

    # opcional: atualiza estoque ao bipar (mas com try pra não travar)
    saldo_int = None
    try:
        bling = BlingService()
        saldo_int = int(bling.obter_estoque(sku))
        cur.execute("UPDATE produtos SET estoque_bling=? WHERE sku=?", (saldo_int, sku))
        db.commit()
    except Exception as e:
        print(f"⚠️ Bling falhou ao obter estoque do SKU {sku}: {e}", flush=True)

    # incrementa bipado
    cur.execute("""
        UPDATE pedido_produtos
           SET quantidade_bipada = quantidade_bipada + 1
         WHERE id = ?
    """, (row["pp_id"],))
    db.commit()

    # totals inline
    totals = cur.execute("""
        SELECT
          SUM(pp.quantidade) as total_qtd,
          SUM(pp.quantidade_bipada) as total_bip
        FROM pedido_produtos pp
        WHERE pp.pedido_id = ?
    """, (row["pedido_id"],)).fetchone()

    total_qtd = int((totals["total_qtd"] if totals else 0) or 0)
    total_bip = int((totals["total_bip"] if totals else 0) or 0)

    faltas = cur.execute("""
        SELECT COUNT(*) as faltando
        FROM pedido_produtos
        WHERE pedido_id = ?
          AND quantidade_bipada < quantidade
    """, (row["pedido_id"],)).fetchone()

    faltando_itens = int((faltas["faltando"] if faltas else 0) or 0)

    if faltando_itens == 0:
        # marca separado e "trava"
        cur.execute("""
            UPDATE pedidos
               SET status='SEPARADO',
                   atualizado_em=CURRENT_TIMESTAMP
             WHERE id=?
        """, (row["pedido_id"],))

        cur.execute("""
            UPDATE pedido_produtos
               SET quantidade_separada = quantidade_bipada,
                   quantidade_bipada = 0
             WHERE pedido_id = ?
        """, (row["pedido_id"],))

        db.commit()

        # após travar, os totals da tela 1 podem voltar ao 0 bipado
        total_bip = 0

    db.close()
    return jsonify({
        "success": True,
        "message": "Bipado com sucesso!",
        "saldo": saldo_int,
        "ml_id": row["ml_id"],
        "total_quantidade": total_qtd,
        "total_bipado": total_bip,
        "faltando_itens": faltando_itens,
        "pedido_fechado": (faltando_itens == 0),
    })


@main_routes.route("/scanner/descobrir_pedido", methods=["POST"])
def scanner_descobrir_pedido():

    data = request.get_json(silent=True) or {}
    sku = str(data.get("sku") or "").strip()

    if not sku:
        return jsonify({"success": False}), 400

    db = get_db()

    rows = db.execute("""
        SELECT
            p.ml_id,
            p.cliente_nome
        FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        JOIN pedidos p ON p.id = pp.pedido_id
        WHERE pr.sku = ?
        AND p.status = 'SEPARADO'
        AND pp.quantidade_bipada < pp.quantidade
    """, (sku,)).fetchall()

    db.close()

    pedidos = []
    for r in rows:
        pedidos.append({
            "ml_id": r["ml_id"],
            "cliente_nome": r["cliente_nome"]
        })

    return jsonify({
        "success": True,
        "pedidos": pedidos
    })

# ============================================================
# TELA 2 - SCANNER (conferência)
# ============================================================

@main_routes.route("/scanner")
def scanner():
    pedidos = _load_pedidos("SEPARADO")
    stats = {
        "total_pedidos": len(pedidos),
        "total_itens": sum(int(p.get("total_quantidade") or 0) for p in pedidos),
        "total_bipado": sum(int(p.get("total_bipado") or 0) for p in pedidos),
    }
    return render_template("scanner.html", pedidos=pedidos, stats=stats)


@main_routes.route("/scanner/pedido_atual")
def scanner_pedido_atual():

    db = get_db()
    cur = db.cursor()

    pedido = cur.execute("""
        SELECT id, ml_id, cliente_nome
        FROM pedidos
        WHERE status = 'SEPARADO'
        ORDER BY criado_em
        LIMIT 1
    """).fetchone()

    if not pedido:
        db.close()
        return jsonify({"success": False})

    pedido_id = pedido["id"]

    itens = cur.execute("""

    
        SELECT
            pr.sku,
            pr.nome,
            pp.quantidade,
            pp.quantidade_bipada,
            pr.estoque_bling,
            pp.parent_nome
        FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        WHERE pp.pedido_id = ?
ORDER BY pr.nome
    """, (pedido_id,)).fetchall()

    itens_json = []
    for i in itens:
        itens_json.append({
            "sku": i["sku"],
            "nome": i["parent_nome"] or i["nome"],
            "quantidade": i["quantidade"],
            "quantidade_conferida": i["quantidade_bipada"],
            "estoque_bling": i["estoque_bling"]
        })

    db.close()

    return jsonify({
        "success": True,
        "pedido": {
            "id": pedido["id"],
            "ml_id": pedido["ml_id"],
            "cliente_nome": pedido["cliente_nome"],
            "itens": itens_json
        }
    })


@main_routes.route("/scanner/bipar", methods=["POST"])
def scanner_bipar():

    data = request.get_json(silent=True) or {}

    sku = str(data.get("sku") or "").strip()
    ml_id = str(data.get("ml_id") or "").strip()

    if not sku or not ml_id:
        return jsonify({"success": False, "error": "Dados inválidos"}), 400

    db = get_db()
    cur = db.cursor()

    row = cur.execute("""
        SELECT
            pp.id as pp_id,
            pp.quantidade,
            pp.quantidade_bipada,
            p.id as pedido_id,
            p.ml_id
        FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        JOIN pedidos p ON p.id = pp.pedido_id
        WHERE pr.sku = ?
        AND p.ml_id = ?
        AND p.status = 'SEPARADO'
        AND pp.quantidade_bipada < pp.quantidade
        LIMIT 1
    """, (sku, ml_id)).fetchone()

    if not row:
        db.close()
        return jsonify({"success": False, "error": "Item não pertence ao pedido"}), 404

    # atualiza bipagem
    cur.execute("""
        UPDATE pedido_produtos
        SET quantidade_bipada = quantidade_bipada + 1
        WHERE id = ?
    """, (row["pp_id"],))

    # verifica faltantes
    faltantes = cur.execute("""
        SELECT COUNT(*)
        FROM pedido_produtos
        WHERE pedido_id = ?
        AND quantidade_bipada < quantidade
    """, (row["pedido_id"],)).fetchone()[0]

    db.commit()

    # pedido ainda não terminou
    if faltantes > 0:
        db.close()

        return jsonify({
            "success": True,
            "pedido_fechado": False,
            "faltando_itens": faltantes
        })

    # ---------------------------
    # PEDIDO FINALIZADO
    # ---------------------------

    try:

        from services.emitir_via_rpa import emitir_nfe_por_ml_id


        kit = cur.execute("""
        SELECT parent_sku
        FROM pedido_produtos
        WHERE pedido_id = ?
        AND parent_sku IS NOT NULL
        LIMIT 1
        """, (row["pedido_id"],)).fetchone()

        sku_kit = None

        if kit:
            sku_kit = kit["parent_sku"]
            print(f"📦 Pedido contém KIT → SKU do kit: {sku_kit}", flush=True)

        print(f"🚀 Emitindo NF {ml_id}", flush=True)

        resultado = emitir_nfe_por_ml_id(
            ml_any_id=ml_id,
            sku=sku_kit,
            headless=True
        )

        pdf_path = resultado.get("pdf")

    except Exception as e:

        print("❌ ERRO emissão:", e, flush=True)
        pdf_path = None

    # atualiza status
    cur.execute("""
        UPDATE pedidos
        SET status = 'CONCLUIDO'
        WHERE id = ?
    """, (row["pedido_id"],))

    db.commit()
    db.close()

    return jsonify({
        "success": True,
        "pedido_fechado": True,
        "faltando_itens": 0,
        "pdf": pdf_path
    })

# ============================================================
# Marcar separado manual (botão)
# ============================================================

@main_routes.route("/pedido/<ml_id>/separar", methods=["POST"])
def marcar_separado(ml_id):
    db = get_db()
    cur = db.execute("""
        UPDATE pedidos
        SET status='SEPARADO', atualizado_em=CURRENT_TIMESTAMP
        WHERE ml_id=?
          AND status IN ('PENDENTE','PARCIAL')
    """, (ml_id,))
    db.commit()
    db.close()

    if cur.rowcount == 0:
        return jsonify({"success": False, "error": "Pedido não encontrado ou status inválido"}), 404
    return jsonify({"success": True, "ml_id": ml_id})

serial_bp = Blueprint("serial_bp", __name__)

@serial_bp.post("/serial/add")
def serial_add():
    data = request.get_json(force=True) or {}
    ml_id = str(data.get("ml_id") or "").strip()
    sku = str(data.get("sku") or "").strip()
    serial = str(data.get("serial") or "").strip()

    if not ml_id or not serial:
        return jsonify({"success": False, "error": "ml_id e serial são obrigatórios"}), 400

    store = SerialStore()
    store.add(ml_id, sku or "SEM_SKU", serial)  # se você não quiser sku, pode usar SEM_SKU

    return jsonify({"success": True})

# =========================
# SERVIR PDF PARA IMPRESSÃO
# =========================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

@main_routes.route("/print/<path:filename>")
def print_file(filename):

    path = os.path.join(DOWNLOADS_DIR, filename)

    if not os.path.exists(path):
        abort(404)

    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=False
    )

# =========================
# GERAR PDF DE TESTE
# =========================
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import time

@main_routes.route("/debug/print_test")
def debug_print_test_route():

    nome = f"teste_{int(time.time())}.pdf"
    path = os.path.join(DOWNLOADS_DIR, nome)

    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica", 20)
    c.drawString(100, 700, "TESTE DE IMPRESSAO")
    c.setFont("Helvetica", 14)
    c.drawString(100, 650, "Se voce esta lendo isso")
    c.drawString(100, 620, "o sistema funcionou")
    c.drawString(100, 590, "Impressora: 4BARCODE-4B-2074B")
    c.save()

    return {"pdf": f"/print/{nome}"}