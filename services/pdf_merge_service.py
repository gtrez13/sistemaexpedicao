from io import BytesIO
from PyPDF2 import PdfReader, PdfWriter


def merge_pdfs(pdf1_bytes: bytes, pdf2_bytes: bytes) -> bytes:
    writer = PdfWriter()

    reader1 = PdfReader(BytesIO(pdf1_bytes))
    reader2 = PdfReader(BytesIO(pdf2_bytes))

    for page in reader1.pages:
        writer.add_page(page)

    for page in reader2.pages:
        writer.add_page(page)

    output = BytesIO()
    writer.write(output)

    return output.getvalue()