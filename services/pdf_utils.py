# services/pdf_utils.py
from __future__ import annotations

import io
from typing import Tuple, Optional

from pypdf import PdfReader, PdfWriter, Transformation

# 4x6 em pontos (PDF trabalha em points: 72 pt = 1 inch)
# 4" x 6" = 288 x 432 pt
PAGE_4X6_PT = (288.0, 432.0)


def is_pdf(b: bytes) -> bool:
    return isinstance(b, (bytes, bytearray)) and len(b) >= 4 and b[:4] == b"%PDF"


def _read_pdf(b: bytes) -> PdfReader:
    return PdfReader(io.BytesIO(b))


def _write_pdf(writer: PdfWriter) -> bytes:
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_first_page_only(pdf_bytes: bytes) -> bytes:
    """
    Mantém apenas a primeira página do PDF.
    """
    if not is_pdf(pdf_bytes):
        raise ValueError("pdf_first_page_only: não é PDF")

    r = _read_pdf(pdf_bytes)
    w = PdfWriter()
    if not r.pages:
        return pdf_bytes
    w.add_page(r.pages[0])
    return _write_pdf(w)


def _page_wh(page) -> Tuple[float, float]:
    mb = page.mediabox
    return float(mb.width), float(mb.height)


def _rotate_if_landscape(page) -> None:
    """
    Se estiver landscape, gira 90º pra ficar portrait.
    """
    w, h = _page_wh(page)
    if w > h:
        page.rotate(90)


def normalize_pdf_to_4x6_portrait(
    pdf_bytes: bytes,
    *,
    fill: bool = True,
    rotate_auto: bool = True,
    first_page_only: bool = False,
) -> bytes:
    """
    Normaliza PDF para 4x6 (portrait).
    - rotate_auto: gira páginas landscape
    - fill=True: dá zoom para preencher a folha (pode cortar um pouco nas bordas)
    - fill=False: encaixa inteiro (pode sobrar branco)
    """
    if not is_pdf(pdf_bytes):
        raise ValueError("normalize_pdf_to_4x6_portrait: não é PDF")

    r = _read_pdf(pdf_bytes)
    w_out, h_out = PAGE_4X6_PT

    w = PdfWriter()

    pages = [r.pages[0]] if (first_page_only and r.pages) else list(r.pages)

    for page in pages:
        if rotate_auto:
            _rotate_if_landscape(page)

        src_w, src_h = _page_wh(page)

        # escala
        sx = w_out / src_w
        sy = h_out / src_h
        s = max(sx, sy) if fill else min(sx, sy)

        # centraliza
        new_w = src_w * s
        new_h = src_h * s
        tx = (w_out - new_w) / 2.0
        ty = (h_out - new_h) / 2.0

        # cria página 4x6 vazia e "carimba" a original em cima com transformação
        new_page = w.add_blank_page(width=w_out, height=h_out)
        new_page.merge_transformed_page(
            page,
            Transformation().scale(s, s).translate(tx, ty)
        )

    return _write_pdf(w)


def crop_left_and_normalize_to_4x6(
    pdf_bytes: bytes,
    *,
    side: str = "left",
    crop_ratio: float = 0.50,
    fill: bool = True,
    rotate_auto: bool = True,
    first_page_only: bool = True,
) -> bytes:
    """
    Recorta metade esquerda (ou direita) do PDF e depois normaliza pra 4x6.
    Útil quando a etiqueta vem em A4 com 2 colunas/área branca gigante.

    crop_ratio=0.50 = pega 50% da largura.
    """
    if not is_pdf(pdf_bytes):
        raise ValueError("crop_left_and_normalize_to_4x6: não é PDF")

    r = _read_pdf(pdf_bytes)
    w = PdfWriter()

    pages = [r.pages[0]] if (first_page_only and r.pages) else list(r.pages)

    for p in pages:
        if rotate_auto:
            _rotate_if_landscape(p)

        mb = p.mediabox
        x0, y0, x1, y1 = float(mb.left), float(mb.bottom), float(mb.right), float(mb.top)
        width = x1 - x0

        if side.lower() == "right":
            nx0 = x0 + width * (1.0 - crop_ratio)
            nx1 = x1
        else:
            nx0 = x0
            nx1 = x0 + width * crop_ratio

        # aplica crop
        p.cropbox.lower_left = (nx0, y0)
        p.cropbox.upper_right = (nx1, y1)

        w.add_page(p)

    cropped = _write_pdf(w)
    return normalize_pdf_to_4x6_portrait(
        cropped,
        fill=fill,
        rotate_auto=rotate_auto,
        first_page_only=first_page_only,
    )
