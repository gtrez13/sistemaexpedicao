# services/emitir_via_rpa.py
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from services.bling_nfe_rpa import BlingRPA
from services.bling_service import BlingService
from services.ml_service import MercadoLivreService


# ============================================================
# HELPERS: timeout + retry (pra não travar em requests do Bling)
# ============================================================
def _call_with_timeout(fn, timeout_s: int, label: str):
    """
    Executa uma função em uma thread e estoura timeout se ela pendurar.
    Isso evita travar a rota inteira quando o requests do Bling fica preso.
    """
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=timeout_s)
        except FutureTimeout:
            raise Exception(f"⏱️ Timeout em {label} após {timeout_s}s (Bling/Internet oscilando).")


def _retry(fn, tries: int = 3, sleep_s: float = 1.2, label: str = "op"):
    """
    Retry simples com backoff leve.
    """
    last = None
    for i in range(1, tries + 1):
        try:
            if i > 1:
                print(f"🔁 Retry {i}/{tries} em {label}...")
            return fn()
        except Exception as e:
            last = e
            time.sleep(sleep_s)
    raise last


# ============================================================
# FLUXO PRINCIPAL
# ============================================================
def emitir_nfe_por_ml_id(
    ml_any_id: str,
    sku: str | None = None,
    headless: bool = False,
    debug_skip_vendas_gerar: bool = False,
    debug_numero_nf_manual: str | None = None,
):
    """
    ML_ID (order/pack) -> ORDER -> PV -> pega numero visível -> RPA completo (emitir + imprimir combo)

    ✅ Diferença chave:
    - O número da NF (010xxx) vai ser resolvido via API pelo vínculo PV->NFE,
      evitando depender da tabela do Bling (que tá falhando em headless).
    """
    print("🔁 Resolvendo ML → ORDER...")
    ml = MercadoLivreService()
    bling = BlingService()

    order_id = ml.resolver_ml_para_numero_loja(ml_any_id)
    if not order_id:
        raise Exception("❌ Não consegui converter ML ID para ORDER ID")

    print("✅ numeroLoja =", order_id)

    print("🔎 Buscando PV no Bling...")

    def _buscar_pv():
        return bling.buscar_pedido_venda_id_por_numero_loja(order_id, somente_em_aberto=False)

    pv_id = _retry(
        lambda: _call_with_timeout(_buscar_pv, 25, "buscar PV por numeroLoja"),
        tries=3,
        sleep_s=1.3,
        label="buscar PV",
    )

    if not pv_id:
        print("🔎 Tentando fallback pesado (shipping/nickname)...")

        def _fallback_pv():
            return bling.resolver_pv_id_por_ml_id(ml, order_id)

        pv_id = _retry(
            lambda: _call_with_timeout(_fallback_pv, 25, "fallback PV por ML"),
            tries=2,
            sleep_s=1.5,
            label="fallback PV",
        )

    if not pv_id:
        raise Exception("❌ PV não encontrado no Bling")

    pv_id = int(pv_id)
    print("✅ PV_ID =", pv_id)

    def _detalhe_pv():
        return bling.get_pedido_venda_detalhe(pv_id)

    detalhe = _retry(
        lambda: _call_with_timeout(_detalhe_pv, 25, "get_pedido_venda_detalhe"),
        tries=3,
        sleep_s=1.2,
        label="detalhe PV",
    )

    d = detalhe.get("data") or detalhe
    numero_bling = str(d.get("numero") or d.get("numeroPedido") or d.get("id") or pv_id).strip()

    print("🎯 Número visível no Bling =", numero_bling)
    print("🤖 Iniciando RPA...")

    rpa = BlingRPA(headless=headless)

    # ✅ PASSA pv_id + numero_loja (order_id) pra RPA resolver NF via API
    return rpa.emitir(
        numero_bling,
        pv_id=pv_id,
        numero_loja=str(order_id),
    )