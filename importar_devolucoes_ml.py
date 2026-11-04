#!/usr/bin/env python3
"""
Importa de UMA VEZ as devolucoes do Mercado Livre para o modulo de Devolucoes do WMS (rode no servidor, na
pasta do backend). Espera o limite do ML (429) sozinho e continua ate acabar; pode rodar de novo a qualquer
hora (nao duplica nada: cada reclamacao entra uma unica vez).

    python importar_devolucoes_ml.py            # importa tudo
    python importar_devolucoes_ml.py --custos   # so MOSTRA o frete de volta que o ML cobra (nao grava nada)

Seguro: so le do ML (nao renova token: se vencer, avisa e para) e so grava na tabela 'devolucoes'.
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

BASE = "https://api.mercadolibre.com"
PAUSA = 0.8


def ml_get_factory():
    tok = os.getenv("ML_ACCESS_TOKEN")
    if not tok:
        sys.exit("ML_ACCESS_TOKEN nao encontrado no .env")
    h = {"Authorization": f"Bearer {tok}"}

    def ml_get(path, params=None):
        time.sleep(PAUSA)
        r = requests.get(BASE + path, headers=h, params=params, timeout=30)
        if r.status_code == 401:
            sys.exit("Token do ML vencido agora (o WMS renova sozinho). Rode de novo em alguns minutos.")
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    return ml_get


def importar(ml_get=None, espera_429=75, max_rodadas=60, log=print):
    import devolucoes_routes as d
    ml_get = ml_get or ml_get_factory()
    r = {}
    for n in range(1, max_rodadas + 1):
        r = d.importar_do_ml(ml_get, pausa=0)
        if not r.get("ok"):
            log("ERRO:", r.get("erro"))
            return r
        log(f"rodada {n}: {r['abertas_no_ml']} reclamacoes abertas | voltam pro galpao {r['voltam_pro_galpao']} | "
            f"armazem do ML (FULL) {r['ficam_no_ml']} | sem envio de volta {r['sem_devolucao']} | "
            f"novas agora {r['novas']} | faltam checar {r['pendentes']}")
        if r["pendentes"] == 0:
            break
        if r.get("limite_ml"):
            log(f"   ML pediu calma (429): espero {espera_429}s e continuo...")
            time.sleep(espera_429)
    return r


def resumo(log=print):
    from db import db_session
    with db_session() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM devolucoes GROUP BY status").fetchall()
    log("\nDevolucoes no WMS por situacao:")
    for x in rows:
        log(f"   {x['status']}: {x['n']}")


def custos(ml_get=None, log=print):
    """Diagnostico (NAO grava): mostra o custo do frete de volta de ate 3 devolucoes, para eu ligar o 'custo' automatico."""
    from db import db_session
    ml_get = ml_get or ml_get_factory()
    with db_session() as conn:
        ids = [x["ml_claim_id"] for x in conn.execute(
            "SELECT ml_claim_id FROM devolucoes WHERE ml_claim_id IS NOT NULL ORDER BY id DESC LIMIT 3").fetchall()]
    for cid in ids:
        code, ret = ml_get(f"/post-purchase/v2/claims/{cid}/returns")
        log(f"\nclaim {cid}: HTTP {code}")
        for sh in ((ret or {}).get("shipments") or []):
            sid = sh.get("shipment_id")
            c2, cs = ml_get(f"/shipments/{sid}/costs")
            log(f"  envio de volta {sid} ({sh.get('status')}) custos: HTTP {c2}")
            log("  " + json.dumps(cs, ensure_ascii=False)[:900])


if __name__ == "__main__":
    if "--custos" in sys.argv:
        custos()
    else:
        importar()
        resumo()
