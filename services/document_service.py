# services/document_service.py
from __future__ import annotations

import os
from typing import Dict, List, Optional

from services.bling_service import BlingService, _is_pdf, _is_html
from services.ml_service import MercadoLivreService
from services.pdf_printer_service import PDFPrinterService
from services.html_to_pdf_service import html_bytes_to_pdf_bytes

from services.serial_store import SerialStore

from pypdf import PdfReader, PdfWriter, Transformation
from io import BytesIO


# -------------------------
# PDF helpers
# -------------------------
def _merge_two_pages(pdf_page1: bytes, pdf_page2: bytes) -> bytes:
    """Mescla 2 PDFs (1 página cada) num PDF final com 2 páginas."""
    r1 = PdfReader(BytesIO(pdf_page1))
    r2 = PdfReader(BytesIO(pdf_page2))
    if not r1.pages or not r2.pages:
        raise Exception("merge: um dos PDFs está vazio")

    out = PdfWriter()
    out.add_page(r1.pages[0])
    out.add_page(r2.pages[0])

    bio = BytesIO()
    out.write(bio)
    return bio.getvalue()


def _danfe_to_4x6_landscape_fit(pdf_bytes: bytes) -> bytes:
    """
    Pega 1ª página do DANFE, gira 90° e encaixa em 4x6 landscape (6x4).
    FIT (não corta) pra QR não sumir.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    if not reader.pages:
        raise Exception("PDF DANFE sem páginas.")

    src = reader.pages[0]
    src.rotate(90)

    src_w = float(src.mediabox.width)
    src_h = float(src.mediabox.height)

    dst_w = 6 * 72
    dst_h = 4 * 72

    margin = 0.90
    scale = min(dst_w / src_w, dst_h / src_h) * margin

    new_w = src_w * scale
    new_h = src_h * scale
    tx = (dst_w - new_w) / 2
    ty = (dst_h - new_h) / 2

    # ajuste fino
    tx += 4
    ty += 2

    out = PdfWriter()
    dst = out.add_blank_page(width=dst_w, height=dst_h)

    src.add_transformation(Transformation().scale(scale).translate(tx=tx, ty=ty))
    dst.merge_page(src)

    bio = BytesIO()
    out.write(bio)
    return bio.getvalue()


# -------------------------
# DocumentService
# -------------------------
class DocumentService:
    """
    Fluxo:
      1) Resolve PV pelo ML order id (numeroLoja) -> pv_id
      2) Se PV exige serial:
           - se não tiver serials no SerialStore: retorna PENDENTE_SERIAL (não emite NF-e)
           - se tiver: anexa seriais em observacoes no PV e libera emissão NF-e
      3) Se NF-e não existir: cria+envia+aguarda autorização (quando permitido)
      4) Se NF-e autorizada: baixa DANFE simplificado
      5) Etiqueta ML: só baixa/imprime quando shipment estiver imprimível (não invoice_pending)
      6) Imprime combo (DANFE + etiqueta) só quando ambos existirem
    """

    @staticmethod
    def registrar_seriais(ml_id: str, serials_by_sku: Dict[str, List[str]]) -> dict:
        """
        Chame isso depois que o operador bipa/cola os seriais.
        - salva no SerialStore
        - resolve PV e anexa os seriais em observacoes do PV
        """
        ml_id = str(ml_id or "").strip()
        if not ml_id:
            return {"success": False, "status": "INPUT_ERROR", "errors": ["ml_id vazio"]}

        # normaliza payload
        cleaned: Dict[str, List[str]] = {}
        for sku, arr in (serials_by_sku or {}).items():
            sku = str(sku or "").strip()
            if not sku:
                continue
            out = []
            for s in (arr or []):
                s = str(s or "").strip()
                if s:
                    out.append(s)
            if out:
                cleaned[sku] = out

        if not cleaned:
            return {"success": False, "status": "INPUT_ERROR", "errors": ["serials_by_sku vazio"]}

        serial_store = SerialStore()
        serial_store.set(ml_id, cleaned)

        bling = BlingService()

        pv_id, nfe_id = bling.resolver_pv_e_nfe_por_ml_order_id(ml_id)
        if not pv_id:
            return {
                "success": False,
                "status": "PV_NOT_FOUND",
                "errors": ["Não encontrei PV no Bling para esse ML ID."],
            }

        # anexa nas observacoes (agora funcionando via PUT)
        bling.anexar_seriais_em_observacoes(int(pv_id), ml_id, cleaned)

        return {
            "success": True,
            "status": "SERIALS_SAVED_AND_ATTACHED",
            "ml_id": ml_id,
            "pv_id": int(pv_id),
            "nfe_id": int(nfe_id) if nfe_id else None,
            "serials": cleaned,
        }

    @staticmethod
    def gerar_documentos(
        ml_id: str,
        ml_shipping_id: Optional[str] = None,
        bling_pv_id: Optional[int] = None,
    ) -> dict:
        mode = (os.getenv("DOCS_MODE") or "REAL").upper()
        printer_name = (os.getenv("PRINTER_NAME") or "").strip()

        results: list[dict] = []
        errors: list[str] = []
        warnings: list[str] = []

        ml_id = str(ml_id or "").strip()
        ml_shipping_id = str(ml_shipping_id or "").strip() if ml_shipping_id else None

        if not ml_id:
            return {
                "success": False,
                "status": "INPUT_ERROR",
                "mode": mode,
                "results": [],
                "errors": ["ml_id vazio"],
                "warnings": [],
                "can_conclude": False,
            }

        if mode == "REAL" and not printer_name:
            return {
                "success": False,
                "status": "CONFIG_ERROR",
                "mode": mode,
                "results": [],
                "errors": ["PRINTER_NAME não definido"],
                "warnings": [],
                "can_conclude": False,
            }

        bling = BlingService()
        ml = MercadoLivreService()
        serial_store = SerialStore()

        # 1) resolve PV + NF-e (se existir)
        pv_id: Optional[int] = int(bling_pv_id) if bling_pv_id else None
        nfe_id: Optional[int] = None

        try:
            if not pv_id:
                pv_id, nfe_id = bling.resolver_pv_e_nfe_por_ml_order_id(ml_id)
            else:
                # se PV veio direto, tenta achar NF-e vinculada
                nfe_id = bling.encontrar_nfe_id_por_pedido_venda(int(pv_id), numero_loja=ml_id)

            if not pv_id:
                return {
                    "success": False,
                    "status": "PV_NOT_FOUND",
                    "mode": mode,
                    "results": [],
                    "errors": ["Não encontrei PV no Bling para esse ML ID."],
                    "warnings": [],
                    "can_conclude": False,
                }
        except Exception as e:
            return {
                "success": False,
                "status": "PV_RESOLVE_FAILED",
                "mode": mode,
                "results": [],
                "errors": [f"Falha resolvendo PV/NF-e: {e}"],
                "warnings": [],
                "can_conclude": False,
            }

        # 2) trava de serial (ANTES de emitir NF-e)
        exige_serial = False
        try:
            exige_serial = bool(bling.pedido_venda_exige_serial(int(pv_id)))
        except Exception:
            exige_serial = False

        if exige_serial:
            serials_by_sku = serial_store.get(ml_id)  # dict ou None

            if not serials_by_sku:
                # aqui sua UI abre os campos pra bipar/colar
                return {
                    "success": False,
                    "status": "PENDENTE_SERIAL",
                    "mode": mode,
                    "pv_id": int(pv_id),
                    "nfe_id": int(nfe_id) if nfe_id else None,
                    "serial_required": True,
                    "results": results,
                    "errors": [],
                    "warnings": ["Pedido exige NÚMERO DE SÉRIE. Cole/bipe e registre antes de emitir NF-e."],
                    "can_conclude": False,
                }

            # se tem serial salvo, garante que está anexado nas observações
            try:
                bling.anexar_seriais_em_observacoes(int(pv_id), ml_id, serials_by_sku)
                results.append({"doc": "SERIAIS_OBSERVACOES", "printed": False, "source": "Bling PV observacoes"})
            except Exception as e:
                return {
                    "success": False,
                    "status": "FALHA_ANEXAR_SERIAL",
                    "mode": mode,
                    "pv_id": int(pv_id),
                    "nfe_id": int(nfe_id) if nfe_id else None,
                    "serial_required": True,
                    "results": results,
                    "errors": [f"Não consegui anexar seriais no PV: {e}"],
                    "warnings": [],
                    "can_conclude": False,
                }

        # 3) emitir NF-e (se não existe)
        needs_robot = False
        try:
            # tenta extrair nfe dentro do PV
            try:
                nfe_from_pv = bling.extrair_nfe_id_do_pedido_venda(int(pv_id))
                if nfe_from_pv:
                    nfe_id = int(nfe_from_pv)
            except Exception:
                pass

            if not nfe_id:
                nfe_id = bling.emitir_nfe_do_pv(int(pv_id), timeout_s=180)
                results.append({"doc": "NFE", "printed": False, "source": "Bling emitir_nfe_do_pv"})

        except Exception as e:
            tipo = bling.classificar_erro_emissao_nfe(e)
            if tipo == "NEEDS_ROBOT_OR_MANUAL":
                needs_robot = True
                warnings.append(
                    "NF-e via API falhou por falta de dados/cadastro. "
                    "Acione o robô para abrir o Bling e clicar em GERAR (motor do Bling). "
                    f"Detalhe: {e}"
                )
            else:
                errors.append(f"NF-e falhou (criar/enviar/autorizar): {e}")
        # 4) DANFE (só se NF-e autorizada)
        danfe_pdf_1pg: Optional[bytes] = None
        try:
            if nfe_id and bling.nfe_esta_autorizada(int(nfe_id)):
                danfe_bytes = bling.baixar_danfe_simplificado_por_nfe(int(nfe_id))
                danfe_source = "Bling_NFE_simplificado"

                if _is_html(danfe_bytes) and not _is_pdf(danfe_bytes):
                    danfe_bytes = html_bytes_to_pdf_bytes(danfe_bytes)
                    danfe_source += "_html2pdf"

                if not _is_pdf(danfe_bytes):
                    raise Exception(f"DANFE não virou PDF. source={danfe_source} bytes={len(danfe_bytes or b'')}")

                danfe_pdf_1pg = _danfe_to_4x6_landscape_fit(danfe_bytes)
                results.append({"doc": "DANFE_SIMPLIFICADO", "printed": False, "source": danfe_source})
            else:
                warnings.append("NF-e ainda não autorizada: não vou imprimir DANFE oficial.")
        except Exception as e:
            errors.append(f"DANFE falhou: {e}")

        # 5) etiqueta ML (só se shipment estiver imprimível)
        etiqueta_pdf: Optional[bytes] = None
        try:
            if not ml_shipping_id:
                warnings.append("Sem ml_shipping_id: não dá pra tentar etiqueta do ML.")
            else:
                etiqueta_pdf = ml.baixar_etiqueta_pdf_4x6(ml_shipping_id)
                if not _is_pdf(etiqueta_pdf):
                    raise Exception(f"Etiqueta ML não é PDF (bytes={len(etiqueta_pdf or b'')})")
                results.append({"doc": "ETIQUETA_ML", "printed": False, "source": "MercadoLivre label"})
        except Exception as e:
            msg = str(e).lower()
            if "invoice_pending" in msg or "not_printable_status" in msg or "shplab0200" in msg:
                warnings.append(f"Etiqueta ML ainda não imprimível (invoice_pending). {e}")
            else:
                errors.append(f"Etiqueta ML falhou: {e}")

        # 6) imprime combo (só se tiver os 2)
        printed_combo = False
        try:
            if mode == "REAL" and danfe_pdf_1pg and etiqueta_pdf:
                combo = _merge_two_pages(danfe_pdf_1pg, etiqueta_pdf)
                PDFPrinterService.print_pdf_bytes(combo, printer_name=printer_name)
                printed_combo = True

                results.append({"doc": "COMBO_2PAGES", "printed": printer_name, "source": "Merged DANFE+ETIQUETA"})
            else:
                if danfe_pdf_1pg and not etiqueta_pdf:
                    warnings.append("Tenho DANFE, mas sem etiqueta imprimível: não vou imprimir parcial.")
                if etiqueta_pdf and not danfe_pdf_1pg:
                    warnings.append("Tenho etiqueta, mas sem DANFE autorizado: não vou imprimir parcial.")
        except Exception as e:
            errors.append(f"Impressão combo falhou: {e}")

        return {
            "success": (len(errors) == 0) and printed_combo,
            "status": "PRINTED" if printed_combo else "NOT_PRINTED",
            "mode": mode,
            "ml_id": ml_id,
            "ml_shipping_id": ml_shipping_id,
            "pv_id": int(pv_id),
            "nfe_id": int(nfe_id) if nfe_id else None,
            "serial_required": bool(exige_serial),
            "results": results,
            "errors": errors,
            "warnings": warnings,
            "can_conclude": bool(printed_combo),
        }