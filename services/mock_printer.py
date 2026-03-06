# services/mock_printer.py
from pathlib import Path
from datetime import datetime

class MockPrinterService:
    """
    Substitui a impressora real.
    Apenas salva os PDFs na pasta debug_output/.
    """

    @staticmethod
    def print_pdf_bytes(pdf_bytes: bytes, printer_name: str | None = None):
        Path("debug_output").mkdir(exist_ok=True)

        ts = datetime.now().strftime("%H%M%S_%f")
        path = Path(f"debug_output/PRINT_{ts}.pdf")

        with open(path, "wb") as f:
            f.write(pdf_bytes)

        print(f"🧪 MOCK PRINT → salvo em: {path}")