"""
Etiqueta unificada (ML + DANFE simplificado) — LHVMed.

    GET /api/etiqueta-unificada/<ml_id>        -> {"success": true, "zpl": "...", "nf": {...}}  (pra Elgin via QZ)
    GET /api/etiqueta-unificada/<ml_id>.png    -> a imagem (prévia / debug)

Desligar sem reinstalar nada: ETIQUETA_UNIFICADA=0 no drop-in do serviço (a tela volta a imprimir DANFE A4 + etiqueta).
Só lê ML. Qualquer falha devolve success=false e a tela cai no fluxo antigo sozinha.
"""
from __future__ import annotations

import os

from flask import Blueprint, Response, jsonify
from flask_login import login_required

etiqueta_routes = Blueprint("etiqueta_routes", __name__)


def _ligada() -> bool:
    return os.environ.get("ETIQUETA_UNIFICADA", "1").strip().lower() not in ("0", "false", "nao", "não", "off")


@etiqueta_routes.get("/etiqueta-unificada/<ml_id>")
@login_required
def etiqueta_unificada(ml_id):
    if not _ligada():
        return jsonify({"success": False, "error": "desligada (ETIQUETA_UNIFICADA=0)"}), 404
    try:
        from etiqueta_unificada import gerar, Falha
        r = gerar(str(ml_id).strip())
        return jsonify({"success": True, "zpl": r["zpl"], "nf": r["nf"], "ml": r["ml"]})
    except Exception as e:
        print(f"⚠️ etiqueta unificada {ml_id}: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 502


@etiqueta_routes.get("/etiqueta-unificada/<ml_id>.png")
@login_required
def etiqueta_unificada_png(ml_id):
    try:
        from etiqueta_unificada import gerar
        r = gerar(str(ml_id).strip())
        return Response(r["png"], mimetype="image/png")
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502
