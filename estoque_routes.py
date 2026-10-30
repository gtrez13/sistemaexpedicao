"""
Atualiza o estoque do Bling SOB DEMANDA (ex.: logo depois do "Sincronizar ML").

    POST /api/estoque/atualizar   dispara estoque_sync.py em segundo plano e responde NA HORA
    GET  /api/estoque/status      {rodando: true|false}  (o front espera acabar e recarrega a lista)

Não faz nenhuma chamada ao Bling dentro da requisição (o servidor web tem 1 worker): só dispara o script
independente, o mesmo do cron, com o mesmo bloqueio (flock) — nunca rodam dois ao mesmo tempo.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time

from flask import Blueprint, jsonify
from flask_login import login_required

estoque_routes = Blueprint("estoque_routes", __name__)

LOCK = "/tmp/estoque_sync.lock"
AQUI = os.path.dirname(os.path.abspath(__file__))
MIN_ENTRE_DISPAROS_S = 20
_ultimo = 0.0


def _rodando() -> bool:
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o666)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


@estoque_routes.get("/estoque/status")
@login_required
def status():
    return jsonify({"rodando": _rodando()})


@estoque_routes.post("/estoque/atualizar")
@login_required
def atualizar():
    global _ultimo
    script = os.path.join(AQUI, "estoque_sync.py")
    if not os.path.exists(script):
        return jsonify({"iniciado": False, "motivo": "estoque_sync.py não instalado"})
    if _rodando():
        return jsonify({"iniciado": False, "rodando": True})
    if time.time() - _ultimo < MIN_ENTRE_DISPAROS_S:
        return jsonify({"iniciado": False, "recente": True})
    _ultimo = time.time()
    env = dict(os.environ)
    env.setdefault("BLING_DEPOSITO_ID", "10488672491")
    try:
        log = open(os.path.expanduser("~/nexuswms/estoque_sync.log"), "a")
    except OSError:
        log = subprocess.DEVNULL
    subprocess.Popen(["flock", "-n", LOCK, sys.executable, script], cwd=AQUI, env=env,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return jsonify({"iniciado": True})
