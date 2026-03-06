# services/zpl_utils.py
from __future__ import annotations
import re

# 4x6 em 203 DPI (8 dpmm) -> largura 4" * 203 = 812 dots, altura 6" * 203 = 1218 dots
# 4x6 em 300 DPI (12 dpmm) -> 1200 x 1800 dots
DPI_TO_4X6 = {
    203: (812, 1218),
    300: (1200, 1800),
}

_XA_XZ_RE = re.compile(r"\^XA.*?\^XZ", re.DOTALL)

def split_zpl_jobs(zpl: str) -> list[str]:
    """Quebra um payload com múltiplos ^XA...^XZ em jobs separados."""
    if not zpl:
        return []
    jobs = _XA_XZ_RE.findall(zpl)
    if jobs:
        return [j.strip() for j in jobs if j.strip()]
    # fallback: se não achar, devolve tudo como 1 job
    return [zpl.strip()]

def _ensure_cmd(job: str, cmd: str, value: str) -> str:
    """
    Garante um comando (ex: ^PW812) dentro do ^XA...^XZ.
    Se já existir, substitui.
    """
    # substitui se já tiver
    pat = re.compile(rf"\{cmd}\d+")
    if pat.search(job):
        job = pat.sub(f"{cmd}{value}", job, count=1)
        return job

    # injeta logo depois do ^XA
    return job.replace("^XA", f"^XA\n{cmd}{value}\n", 1)

def normalize_zpl_to_4x6_portrait(job: str, dpi: int = 203) -> str:
    """
    Força:
      - ^PW (print width)
      - ^LL (label length)
      - ^LH0,0 (label home)
      - remove ^FW (field orientation global) se existir (isso costuma girar)
    """
    job = (job or "").strip()
    if not job:
        return job

    w, h = DPI_TO_4X6.get(int(dpi), DPI_TO_4X6[203])

    # remove orientação global que costuma causar "deitado"
    job = re.sub(r"\^FW[A-Z]", "", job)

    # força home/origem
    # se já tem ^LHx,y, substitui por ^LH0,0
    if re.search(r"\^LH-?\d+,\s*-?\d+", job):
        job = re.sub(r"\^LH-?\d+,\s*-?\d+", "^LH0,0", job, count=1)
    else:
        job = job.replace("^XA", "^XA\n^LH0,0\n", 1)

    # força width/length
    job = _ensure_cmd(job, "^PW", str(w))
    job = _ensure_cmd(job, "^LL", str(h))

    # (opcional) garante modo de mídia/tear-off padrão
    # job = _ensure_cmd(job, "^MM", "T")  # T = tear-off

    return job

def choose_job_by_hint(jobs: list[str], hint: str) -> str | None:
    """
    Pega 1 job que contenha um texto (ex: 'Envio Flex' ou 'DANFE').
    """
    if not jobs:
        return None
    hint_low = (hint or "").lower().strip()
    if not hint_low:
        return jobs[0]
    for j in jobs:
        if hint_low in j.lower():
            return j
    return None
