# services/pdf_4x6.py
from __future__ import annotations

from io import BytesIO
from typing import Optional

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf._page import PageObject


PT_PER_INCH = 72.0

# 4x6 em pontos (portrait)
PAGE_W_4 = 4.0 * PT_PER_INCH   # 288
PAGE_H_6 = 6.0 * PT_PER_INCH   # 432

def keep_only_first_page(pdf_bytes: bytes) -> bytes:
    r = PdfReader(BytesIO(pdf_bytes))
    w = PdfWriter()
    if not r.pages:
        return pdf_bytes
    w.add_page(r.pages[0])
    out = BytesIO()
    w.write(out)
    return out.getvalue()

def normalize_pdf_to_4x6(
    pdf_bytes: bytes,
    *,
    margin_pt: float = 12.0,
    extra_shrink: float = 1.0,
    user_scale: float = 1.0,
    dx_pt: float = 0.0,
    dy_pt: float = 0.0,
    force_portrait: bool = True,
) -> bytes:
    """
    Normaliza QUALQUER PDF para página 4x6 (em pontos), centralizando e escalando o conteúdo.

    Parâmetros (compatível com seu document_service.py):
      - margin_pt: margem interna em pontos (pt)
      - extra_shrink: multiplicador de escala final (ex: 0.975 pra encolher; 2.02 pra aumentar)
      - user_scale: outro multiplicador (usado no modo combo por ENV)
      - dx_pt/dy_pt: ajuste fino de posição (pt) após centralizar
      - force_portrait: sempre gera 4x6 em pé (recomendado pra sua térmica)

    Retorna:
      - pdf_bytes normalizado para 4x6, uma página por página original.
    """

    if not isinstance(pdf_bytes, (bytes, bytearray)) or len(pdf_bytes) < 8:
        raise ValueError("pdf_bytes inválido/vazio")

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()

    # Tamanho alvo (4x6)
    target_w = PAGE_W_4
    target_h = PAGE_H_6

    for src in reader.pages:
        mb = src.mediabox
        src_w = float(mb.width)
        src_h = float(mb.height)

        # Se quiser respeitar orientação original, você poderia trocar aqui,
        # mas para térmica 4x6 normalmente é melhor forçar portrait.
        out_w, out_h = (target_w, target_h) if force_portrait else (
            (target_h, target_w) if src_w > src_h else (target_w, target_h)
        )

        # área útil considerando margem
        avail_w = max(1.0, out_w - 2.0 * float(margin_pt))
        avail_h = max(1.0, out_h - 2.0 * float(margin_pt))

        # escala base para caber dentro da área útil
        base_scale = min(avail_w / src_w, avail_h / src_h)

        # ajustes finais: user_scale * extra_shrink
        scale = base_scale * float(user_scale) * float(extra_shrink)

        # dimensões após escala
        new_w = src_w * scale
        new_h = src_h * scale

        # centraliza
        tx = (out_w - new_w) / 2.0
        ty = (out_h - new_h) / 2.0

        # aplica ajuste fino (dx/dy)
        tx += float(dx_pt)
        ty += float(dy_pt)

        # cria página alvo e mescla a original transformada
        blank = PageObject.create_blank_page(width=out_w, height=out_h)

        transform = Transformation().scale(scale, scale).translate(tx, ty)
        blank.merge_transformed_page(src, transform)

        writer.add_page(blank)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()