# services/html_to_pdf_service.py
import os
import tempfile
from playwright.sync_api import sync_playwright

def html_bytes_to_pdf_bytes(html_bytes: bytes, width_mm: int = 100, height_mm: int = 150) -> bytes:
    html_text = html_bytes.decode("utf-8", errors="ignore")

    with tempfile.TemporaryDirectory() as td:
        html_path = os.path.join(td, "doc.html")
        pdf_path = os.path.join(td, "doc.pdf")

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_text)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 800, "height": 1200})
            page.goto(f"file:///{html_path}", wait_until="networkidle")

            page.pdf(
                path=pdf_path,
                width=f"{width_mm}mm",
                height=f"{height_mm}mm",
                print_background=True,
                margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"},
                prefer_css_page_size=True,
            )
            browser.close()

        with open(pdf_path, "rb") as f:
            return f.read()
