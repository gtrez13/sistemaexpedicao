# services/pdf_printer_service.py
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


class PDFPrinterService:
    """
    Impressão PDF no Windows via SumatraPDF (CLI).
    Imprime TODAS as páginas do PDF (importante para PDF combo: DANFE + Etiqueta).
    """

    @staticmethod
    def _get_sumatra_exe() -> str:
        exe = (os.getenv("SUMATRA_EXE") or "").strip()
        if exe and Path(exe).exists():
            return exe

        # tenta caminhos comuns
        candidates = [
            r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
            r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c

        raise Exception(
            "SumatraPDF.exe não encontrado. Instale o SumatraPDF e configure SUMATRA_EXE no .env"
        )

    @staticmethod
    def print_pdf_bytes(pdf_bytes: bytes, printer_name: str, *, copies: int = 1) -> None:
        if not printer_name or not str(printer_name).strip():
            raise Exception("printer_name vazio")

        if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
            raise Exception("Conteúdo não é PDF válido")

        sumatra = PDFPrinterService._get_sumatra_exe()

        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "doc.pdf"
            pdf_path.write_bytes(pdf_bytes)

            # -print-to: nome exato da impressora no Windows
            # -print-settings: pode incluir "fit" e "noscale" etc. (depende do driver)
            # -silent: sem UI
            # -exit-when-done: encerra
            cmd = [
                sumatra,
                "-silent",
                "-exit-when-done",
                "-print-to", str(printer_name),
                str(pdf_path),
            ]

            # cópias: chamar N vezes é mais confiável do que flags esquisitas
            for _ in range(max(1, int(copies))):
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    raise Exception(
                        f"Falha ao imprimir via SumatraPDF (code={r.returncode}). "
                        f"stdout={r.stdout[-300:]} stderr={r.stderr[-300:]}"
                    )