#!/usr/bin/env bash
# Instalador do modulo PICKING (separacao unificada + expedicao em lote). Rode DENTRO da pasta do backend (app.py).
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

cat > picking_routes.py <<'PICKEOF'
"""
Módulo PICKING (backend) — separação unificada e expedição travada. LHVMed.

Endpoints (url_prefix="/api"; exigem login):
    POST /api/separacao/bipar       {sku, ml_id?, quantidade=1}
         Bipa N unidades do SKU num pedido PENDENTE. Sem ml_id escolhe o pedido mais antigo que precisa do SKU
         (FIFO). NUNCA passa do que falta no pedido (o excedente é devolvido em "sobrou"). Fecha o pedido
         (SEPARADO) só quando TODOS os itens estão completos. Devolve tudo mastigado para o painel.
    POST /api/expedicao/bipar-lote  {sku, ml_id, quantidade}
         Soma N unidades no pedido SEPARADO sem nunca completar o item (deixa ao menos 1 faltando): a última
         unidade vai pelo /scanner/bipar de sempre, que é quem emite a NF-e e imprime. Assim o fluxo de fechamento
         da expedição continua sendo o original.

Respeita: parent_sku (bipar pelo componente ou pelo kit), audit_log (action "bipar", quantidade N) para o
Painel TV e a rastreabilidade.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

picking_routes = Blueprint("picking_routes", __name__)


def _log(acao, ml_id, sku, nome, qtd, extra=None):
    try:
        from services.audit_service import log_action
        uid = current_user.id if getattr(current_user, "is_authenticated", False) else None
        meta = {"sku": sku, "nome": nome or "", "quantidade": int(qtd)}
        if extra:
            meta.update(extra)
        log_action(user_id=uid, action=acao, entity_type="pedido", entity_id=str(ml_id), metadata=meta,
                   ip_address=request.remote_addr)
    except Exception as e:  # nunca trava o bip
        print(f"⚠️ audit picking falhou: {e}", flush=True)


def _itens_do_pedido(conn, pedido_id):
    rows = conn.execute("""
        SELECT pr.sku, pr.nome, pp.quantidade, pp.quantidade_bipada, pp.parent_sku, pp.parent_nome
          FROM pedido_produtos pp JOIN produtos pr ON pr.id = pp.produto_id
         WHERE pp.pedido_id = ? ORDER BY pp.id
    """, (pedido_id,)).fetchall()
    itens = [{"sku": r["sku"], "nome": r["nome"] or r["sku"], "quantidade": int(r["quantidade"] or 0),
              "quantidade_bipada": int(r["quantidade_bipada"] or 0), "parent_sku": r["parent_sku"],
              "parent_nome": r["parent_nome"]} for r in rows]
    for it in itens:
        it["falta"] = max(0, it["quantidade"] - it["quantidade_bipada"])
    return itens


def _resumo_pedido(itens):
    return {
        "total": sum(i["quantidade"] for i in itens),
        "bipado": sum(i["quantidade_bipada"] for i in itens),
        "falta": sum(i["falta"] for i in itens),
        "itens": itens,
        "itens_faltando": [{"sku": i["sku"], "nome": i["nome"], "falta": i["falta"], "parent_nome": i["parent_nome"]}
                           for i in itens if i["falta"] > 0],
    }


def _linha(conn, sku, ml_id, status):
    """Linha de pedido_produtos que ainda falta para esse SKU (pelo componente ou pelo kit), FIFO."""
    sql = """
        SELECT pp.id AS pp_id, pp.pedido_id, p.ml_id, p.cliente_nome, p.origem, pr.sku, pr.nome,
               pp.quantidade, pp.quantidade_bipada, pp.parent_sku, pp.parent_nome
          FROM pedido_produtos pp
          JOIN produtos pr ON pr.id = pp.produto_id
          JOIN pedidos  p  ON p.id  = pp.pedido_id
         WHERE (UPPER(pr.sku) = UPPER(?) OR UPPER(COALESCE(pp.parent_sku,'')) = UPPER(?))
           AND p.status = ?
           AND pp.quantidade_bipada < pp.quantidade
    """
    args = [sku, sku, status]
    if ml_id:
        sql += " AND p.ml_id = ?"
        args.append(ml_id)
    sql += " ORDER BY p.criado_em ASC, p.id ASC, pp.id ASC LIMIT 1"
    return conn.execute(sql, tuple(args)).fetchone()


@picking_routes.post("/separacao/bipar")
@login_required
def separacao_bipar():
    d = request.get_json(silent=True) or {}
    sku = str(d.get("sku") or "").strip()
    ml_id = str(d.get("ml_id") or "").strip() or None
    try:
        qtd = int(d.get("quantidade") or 1)
    except (TypeError, ValueError):
        qtd = 1
    if not sku:
        return jsonify({"success": False, "error": "SKU vazio"}), 400
    if qtd < 1 or qtd > 5000:
        return jsonify({"success": False, "error": "Quantidade inválida (1 a 5000)"}), 400

    with db_session() as conn:
        row = _linha(conn, sku, ml_id, "PENDENTE")
        if not row:
            if ml_id:
                p = conn.execute("SELECT id, cliente_nome FROM pedidos WHERE ml_id=? AND status='PENDENTE'", (ml_id,)).fetchone()
                if p:
                    itens = _itens_do_pedido(conn, p["id"])
                    return jsonify({"success": False, "error": f"{sku} não é do pedido #{ml_id} (ou já está completo nele)",
                                    "detalhe": p["cliente_nome"], "pedido": _resumo_pedido(itens)}), 404
            return jsonify({"success": False, "error": f"{sku} não está em nenhum pedido pendente"}), 404

        falta = int(row["quantidade"]) - int(row["quantidade_bipada"])
        dar = min(qtd, falta)                     # NUNCA passa do que o pedido precisa
        conn.execute("UPDATE pedido_produtos SET quantidade_bipada = quantidade_bipada + ? WHERE id = ?", (dar, row["pp_id"]))

        itens = _itens_do_pedido(conn, row["pedido_id"])
        fechado = all(i["falta"] == 0 for i in itens)
        if fechado:
            conn.execute("UPDATE pedidos SET status='SEPARADO', atualizado_em=CURRENT_TIMESTAMP WHERE id=?", (row["pedido_id"],))
            conn.execute("UPDATE pedido_produtos SET quantidade_separada = quantidade_bipada, quantidade_bipada = 0 WHERE pedido_id = ?",
                         (row["pedido_id"],))
        item = next((i for i in itens if i["sku"] == row["sku"] and (i["parent_sku"] or "") == (row["parent_sku"] or "")), None)

    _log("bipar", row["ml_id"], row["sku"], row["nome"], dar, {"parent_sku": row["parent_sku"], "parent_nome": row["parent_nome"], "lote": dar > 1})
    return jsonify({
        "success": True, "ml_id": row["ml_id"], "cliente": row["cliente_nome"], "origem": row["origem"] or "ML",
        "bipado_agora": dar, "sobrou": qtd - dar,
        "item": item, "pedido": _resumo_pedido(itens), "pedido_fechado": fechado,
    })


@picking_routes.post("/expedicao/bipar-lote")
@login_required
def expedicao_bipar_lote():
    d = request.get_json(silent=True) or {}
    sku = str(d.get("sku") or "").strip()
    ml_id = str(d.get("ml_id") or "").strip()
    try:
        qtd = int(d.get("quantidade") or 0)
    except (TypeError, ValueError):
        qtd = 0
    if not sku or not ml_id:
        return jsonify({"success": False, "error": "sku e ml_id obrigatórios"}), 400
    if qtd < 1 or qtd > 5000:
        return jsonify({"success": False, "error": "Quantidade inválida (1 a 5000)"}), 400

    with db_session() as conn:
        row = _linha(conn, sku, ml_id, "SEPARADO")
        if not row:
            return jsonify({"success": False, "error": f"{sku} não é do pedido #{ml_id} (ou já está completo nele)"}), 404
        falta = int(row["quantidade"]) - int(row["quantidade_bipada"])
        somar = min(qtd, falta - 1)               # deixa 1 para o /scanner/bipar fechar do jeito de sempre
        if somar > 0:
            conn.execute("UPDATE pedido_produtos SET quantidade_bipada = quantidade_bipada + ? WHERE id = ?", (somar, row["pp_id"]))
        itens = _itens_do_pedido(conn, row["pedido_id"])

    if somar > 0:
        _log("bipar", row["ml_id"], row["sku"], row["nome"], somar, {"parent_sku": row["parent_sku"], "lote": True, "expedicao": True})
    return jsonify({"success": True, "ml_id": row["ml_id"], "somado": max(0, somar), "pedido": _resumo_pedido(itens)})
PICKEOF
python3 -m py_compile picking_routes.py && echo ">> picking_routes.py ok"

python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "picking_routes" in s:
    print(">> app.py ja tinha o picking_routes (ok)")
else:
    linha = '    from picking_routes import picking_routes as picking_bp\n    app.register_blueprint(picking_bp, url_prefix="/api")\n'
    ancoras = ['    app.register_blueprint(devolucoes_bp, url_prefix="/api")\n',
               '    app.register_blueprint(naoachado_bp, url_prefix="/api")\n',
               '    app.register_blueprint(fefo_bp, url_prefix="/api")\n',
               '    app.register_blueprint(mapa_bp, url_prefix="/api")\n',
               "    app.register_blueprint(api_routes)\n"]
    for a in ancoras:
        if a in s:
            open(f + ".bak_picking", "w", encoding="utf-8").write(s)
            open(f, "w", encoding="utf-8").write(s.replace(a, a + linha, 1))
            print(">> app.py patchado (backup em app.py.bak_picking)")
            break
    else:
        print("!! Nao achei onde registrar. Adicione dentro de create_app():"); print(linha); raise SystemExit(1)
PYEOF
python3 -m py_compile app.py && echo ">> app.py COMPILOU"
echo ""
echo "OK. Reinicie: sudo systemctl restart wms-russia.service ; teste: curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/separacao/bipar   (deve dar 401)"
echo "Reverter: cp app.py.bak_picking app.py && sudo systemctl restart wms-russia.service"
