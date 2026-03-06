import os
import subprocess
import time
from dotenv import load_dotenv

load_dotenv()

class WindowsPrintService:

    @staticmethod
    def _ps(cmd: str) -> subprocess.CompletedProcess:
        """
        Executa PowerShell e retorna stdout/stderr.
        """
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True
        )

    @staticmethod
    def _count_print_jobs(printer_name: str) -> int:
        """
        Conta jobs na fila da impressora (via PowerShell Get-PrintJob).
        Retorna -1 se o comando não estiver disponível/der erro.
        """
        # Escapa aspas
        pn = printer_name.replace("'", "''")
        ps_cmd = f"(Get-PrintJob -PrinterName '{pn}' -ErrorAction SilentlyContinue | Measure-Object).Count"
        r = WindowsPrintService._ps(ps_cmd)
        if r.returncode != 0:
            return -1
        out = (r.stdout or "").strip()
        try:
            return int(out)
        except Exception:
            return -1

    @staticmethod
    def print_pdf(pdf_path: str, printer_name: str, verify_queue: bool = True):
        if not os.path.exists(pdf_path):
            raise Exception(f"Arquivo não encontrado: {pdf_path}")

        sumatra_path = os.getenv("SUMATRA_PATH")

        if not sumatra_path or not os.path.exists(sumatra_path):
            raise Exception("SUMATRA_PATH inválido no .env")

        print(f"🖨️ Imprimindo via Sumatra: {printer_name}")
        print(f"📄 Arquivo: {pdf_path}")

        before = None
        if verify_queue:
            before = WindowsPrintService._count_print_jobs(printer_name)
            if before >= 0:
                print(f"📥 Jobs antes: {before}")
            else:
                print("⚠️ Não consegui ler a fila (Get-PrintJob indisponível/sem permissão). Vou confiar no retorno do Sumatra.")

        # IMPORTANTE: usar 'noscale'
        args = [
            sumatra_path,
            "-print-to", printer_name,
            "-print-settings", "noscale",
            "-silent",
            pdf_path
        ]

        # captura stdout/stderr do Sumatra (se der erro, você vê)
        r = subprocess.run(args, capture_output=True, text=True)
        if r.returncode != 0:
            raise Exception(
                "❌ Sumatra retornou erro\n"
                f"cmd: {' '.join(args)}\n"
                f"stdout: {r.stdout}\n"
                f"stderr: {r.stderr}"
            )

        print("✅ Sumatra aceitou o comando (job enviado pro Windows)")

        if verify_queue and before is not None and before >= 0:
            # espera um pouquinho pro spooler registrar
            time.sleep(0.8)

            after = WindowsPrintService._count_print_jobs(printer_name)
            print(f"📤 Jobs depois: {after}")

            if after == -1:
                print("⚠️ Não consegui confirmar pela fila, mas o Sumatra não retornou erro.")
            elif after > before:
                print("✅ CONFIRMADO: apareceu job novo na fila da impressora.")
            else:
                print("⚠️ Não vi job novo na fila. Pode ter sido MUITO rápido (entrou e saiu), ou o spooler não registrou a tempo.")
                print("   Dica: abra a fila da impressora pra ver histórico/estado.")

        print("🏁 print_pdf finalizado")