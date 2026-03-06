from io import BytesIO
from pypdf import PdfReader, PdfWriter
from pypdf._page import PageObject
from pypdf import Transformation

PT_PER_INCH = 72.0

def normalize_pdf_to_4x6(
    pdf_bytes: bytes,
    margin_pt: float = 12.0,
    extra_shrink: float = 1.0,   # <- AGORA EXISTE
    force_portrait: bool = True,
) -> bytes:
    """
    Normaliza QUALQUER PDF para uma página 4x6 (em pontos), centralizando e escalando o conteúdo.
    - margin_pt: margem interna em pontos
    - extra_shrink: multiplicador final de escala
        * < 1.0 -> encolhe um pouco mais
        * > 1.0 -> aumenta (útil pra etiqueta que veio pequena)
    """

    if not pdf_bytes:
        raise ValueError("pdf_bytes vazio")

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()

    target_w = 4.0 * PT_PER_INCH   # 288
    target_h = 6.0 * PT_PER_INCH   # 432

    for src in reader.pages:
        mb = src.mediabox
        src_w = float(mb.width)
        src_h = float(mb.height)

        # Decide orientação alvo
        if force_portrait:
            out_w, out_h = target_w, target_h
        else:
            # Mantém a "cara" do original
            if src_w > src_h:
                out_w, out_h = target_h, target_w  # landscape 6x4
            else:
                out_w, out_h = target_w, target_h

        avail_w = max(1.0, out_w - 2 * margin_pt)
        avail_h = max(1.0, out_h - 2 * margin_pt)

        # escala base pra caber dentro da área disponível
        base_scale = min(avail_w / src_w, avail_h / src_h)

        # fator extra (se quiser dar “zoom” ou “shrink”)
        scale = base_scale * float(extra_shrink)

        # tamanho após escala
        new_w = src_w * scale
        new_h = src_h * scale

        # centraliza dentro do 4x6
        tx = (out_w - new_w) / 2.0
        ty = (out_h - new_h) / 2.0

        blank = PageObject.create_blank_page(width=out_w, height=out_h)

        # aplica transformação: escala + translate
        transform = Transformation().scale(scale, scale).translate(tx, ty)

        # merge
        blank.merge_transformed_page(src, transform)

        writer.add_page(blank)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()from io import BytesIO
from pypdf import PdfReader, PdfWriter
from pypdf._page import PageObject
from pypdf import Transformation

PT_PER_INCH = 72.0

def normalize_pdf_to_4x6(
    pdf_bytes: bytes,
    margin_pt: float = 12.0,
    extra_shrink: float = 1.0,   # <- AGORA EXISTE
    force_portrait: bool = True,
) -> bytes:
    """
    Normaliza QUALQUER PDF para uma página 4x6 (em pontos), centralizando e escalando o conteúdo.
    - margin_pt: margem interna em pontos
    - extra_shrink: multiplicador final de escala
        * < 1.0 -> encolhe um pouco mais
        * > 1.0 -> aumenta (útil pra etiqueta que veio pequena)
    """

    if not pdf_bytes:
        raise ValueError("pdf_bytes vazio")

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()

    target_w = 4.0 * PT_PER_INCH   # 288
    target_h = 6.0 * PT_PER_INCH   # 432

    for src in reader.pages:
        mb = src.mediabox
        src_w = float(mb.width)
        src_h = float(mb.height)

        # Decide orientação alvo
        if force_portrait:
            out_w, out_h = target_w, target_h
        else:
            # Mantém a "cara" do original
            if src_w > src_h:
                out_w, out_h = target_h, target_w  # landscape 6x4
            else:
                out_w, out_h = target_w, target_h

        avail_w = max(1.0, out_w - 2 * margin_pt)
        avail_h = max(1.0, out_h - 2 * margin_pt)

        # escala base pra caber dentro da área disponível
        base_scale = min(avail_w / src_w, avail_h / src_h)

        # fator extra (se quiser dar “zoom” ou “shrink”)
        scale = base_scale * float(extra_shrink)

        # tamanho após escala
        new_w = src_w * scale
        new_h = src_h * scale

        # centraliza dentro do 4x6
        tx = (out_w - new_w) / 2.0
        ty = (out_h - new_h) / 2.0

        blank = PageObject.create_blank_page(width=out_w, height=out_h)

        # aplica transformação: escala + translate
        transform = Transformation().scale(scale, scale).translate(tx, ty)

        # merge
        blank.merge_transformed_page(src, transform)

        writer.add_page(blank)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()