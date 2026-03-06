# services/printer_service.py
from __future__ import annotations

import os
import re
import time
import win32print

DPI_TO_4X6 = {
    203: (812, 1218),
    300: (1200, 1800),
}

_XA_XZ_RE = re.compile(r"\^XA.*?\^XZ", re.DOTALL)

def split_zpl_jobs(zpl: str) -> list[str]:
    if not zpl:
        return []
    jobs = _XA_XZ_RE.findall(zpl)
    return [j.strip() for j in jobs] if jobs else [zpl.strip()]

def normalize_zpl_to_4x6_portrait(job: str, dpi: int = 203) -> str:
    job = (job or "").strip()
    if not job:
        return job

    w, h = DPI_TO_4X6.get(int(dpi), DPI_TO_4X6[203])

    job = re.sub(r"\^FW[A-Z]", "", job)  # remove orientação global

    if re.search(r"\^LH-?\d+,\s*-?\d+", job):
        job = re.sub(r"\^LH-?\d+,\s*-?\d+", "^LH0,0", job, count=1)
    else:
        job = job.replace("^XA", "^XA\n^LH0,0\n", 1)

    if re.search(r"\^PW\d+", job):
        job = re.sub(r"\^PW\d+", f"^PW{w}", job, count=1)
    else:
        job = job.replace("^XA", f"^XA\n^PW{w}\n", 1)

    if re.search(r"\^LL\d+", job):
        job = re.sub(r"\^LL\d+", f"^LL{h}", job, count=1)
    else:
        job = job.replace("^XA", f"^XA\n^LL{h}\n", 1)

    return job

class ZPLPrinterService:
    """
    PRINT_MODE:
      - TEST: salva ZPL normalizado em debug_prints/
      - PROD: envia RAW (ZPL) direto pra impressora

    ENV:
      PRINT_MODE=TEST|PROD
      PRINTER_NAME="Nome da Impressora" (opcional; usa padrão do Windows)
      ZPL_DPI=203|300
      ZPL_JOB_DELAY_MS=0..
    """

    def __init__(self):
        self.mode = (os.getenv("PRINT_MODE") or "PROD").upper().strip()
        self.printer_name = (os.getenv("PRINTER_NAME") or "").strip() or win32print.GetDefaultPrinter()

        try:
            self.zpl_dpi = int(os.getenv("ZPL_DPI") or "203")
        except Exception:
            self.zpl_dpi = 203

        try:
            self.job_delay_ms = int(os.getenv("ZPL_JOB_DELAY_MS") or "0")
        except Exception:
            self.job_delay_ms = 0

    def _ensure_debug_dir(self):
        os.makedirs("debug_prints", exist_ok=True)

    def print_zpl(self, zpl: str, job_name: str = "LHV-MED ZPL"):
        if not zpl or "^XA" not in zpl:
            raise Exception("ZPL vazio ou inválido (não contém ^XA)")

        jobs = split_zpl_jobs(zpl)
        jobs_norm = [normalize_zpl_to_4x6_portrait(j, dpi=self.zpl_dpi) for j in jobs]
        jobs_norm = [j for j in jobs_norm if j and "^XA" in j and "^XZ" in j]

        if not jobs_norm:
            raise Exception("ZPL após normalização ficou vazio/ inválido.")

        if self.mode == "TEST":
            self._ensure_debug_dir()
            path = os.path.join("debug_prints", f"{job_name}.zpl")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n\n".join(jobs_norm))
            print(f"🧪 TEST: ZPL salvo em {path} (jobs={len(jobs_norm)})", flush=True)
            return {"success": True, "mode": "TEST", "file": path, "jobs": len(jobs_norm)}

        if not self.printer_name:
            raise Exception("Nenhuma impressora configurada (PRINTER_NAME ou padrão do Windows).")

        hprinter = None
        try:
            hprinter = win32print.OpenPrinter(self.printer_name)

            for i, one in enumerate(jobs_norm, start=1):
                doc_name = f"{job_name}_{i}" if len(jobs_norm) > 1 else job_name

                win32print.StartDocPrinter(hprinter, 1, (doc_name, None, "RAW"))
                win32print.StartPagePrinter(hprinter)
                win32print.WritePrinter(hprinter, one.encode("utf-8"))
                win32print.EndPagePrinter(hprinter)
                win32print.EndDocPrinter(hprinter)

                if self.job_delay_ms > 0:
                    time.sleep(self.job_delay_ms / 1000.0)

            print(f"🖨️ ZPL enviado: {job_name} -> {self.printer_name} (jobs={len(jobs_norm)})", flush=True)
            return {"success": True, "mode": "PROD", "printer": self.printer_name, "jobs": len(jobs_norm)}
        finally:
            try:
                if hprinter:
                    win32print.ClosePrinter(hprinter)
            except Exception:
                pass
