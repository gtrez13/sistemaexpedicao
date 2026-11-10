#!/usr/bin/env python3
"""
Sincroniza AGORA as devoluções do WMS com o Mercado Livre (espera o limite 429 sozinho):
  1) atualiza o status do envio de volta (a caminho / entregue) e o destino (nosso galpão x armazém do ML)
  2) importa as que faltam (inclusive reclamação encerrada que ainda espera a revisão do vendedor)
  3) lista o que o ML espera que vocês REVISEM (igual "Para sua revisão" no painel do ML)

    venv/bin/python devolucoes_sincronizar.py
"""
import time
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import os, atexit
import devolucoes_routes as d
from db import db_session

# trava: enquanto este script roda, o site NÃO consulta o ML (senão os dois disputam o limite e tudo dá 429)
open(d.SYNC_LOCK, "w").close()
atexit.register(lambda: os.path.exists(d.SYNC_LOCK) and os.remove(d.SYNC_LOCK))


def req(path, **kw):
    for n in range(10):
        st, j = d._ml_req("GET", path, **kw)
        if st != 429:
            return st, j
        print(f"   (limite do ML: esperando {60 + n * 30} s, tentativa {n + 1}/10)", flush=True); time.sleep(60 + n * 30)
    return st, j


print(">> 1) atualizando envio de volta das devoluções abertas…", flush=True)
with db_session() as c:
    rows = c.execute("SELECT id, ml_claim_id, numero_venda, produto, status, ml_envio, bipado FROM devolucoes "
                     "WHERE status IN ('esperada','armazem_ml') AND ml_claim_id IS NOT NULL "
                     "AND COALESCE(ml_envio,'') NOT IN ('delivered','cancelled') ORDER BY id").fetchall()
print(f"   {len(rows)} ainda sem entrega confirmada para checar", flush=True)
mud = 0
for k, r in enumerate(rows, 1):
    print(f"   [{k}/{len(rows)}] #{r['id']} {str(r['produto'] or '')[:40]}", flush=True)
    st, ret = req(f"/post-purchase/v2/claims/{r['ml_claim_id']}/returns")
    if st != 200 or not isinstance(ret, dict):
        continue
    tem, destino, envio = d._destino_da_devolucao(ret)
    novo_status = "armazem_ml" if destino == "warehouse" else ("esperada" if r["status"] == "armazem_ml" and not r["bipado"] and tem else r["status"])
    if envio != r["ml_envio"] or novo_status != r["status"]:
        with db_session() as c:
            c.execute("UPDATE devolucoes SET ml_envio=?, status=?, atualizado_em=? WHERE id=?", (envio, novo_status, int(time.time()), r["id"]))
        mud += 1
        print(f"   #{r['id']} {str(r['produto'] or '')[:40]:<40} envio {r['ml_envio']} -> {envio} | {r['status']} -> {novo_status}", flush=True)
    time.sleep(0.4)
print(f"   {mud} atualizada(s)\n", flush=True)

print(">> 2) importando as que faltam…", flush=True)
for rodada in range(1, 40):
    res = d.importar_do_ml(lambda p, params=None: req(p, params=params), pausa=0.3)
    if not res.get("ok"):
        print("   ERRO:", res.get("erro")); break
    print(f"   rodada {rodada}: novas {res.get('novas')} | faltam checar {res.get('pendentes', 0)}", flush=True)
    if not res.get("pendentes"):
        break
    time.sleep(5)

print("\n>> 3) o ML espera a REVISÃO de vocês nestas (nosso galpão):", flush=True)
with db_session() as c:
    rows = c.execute("SELECT id, ml_claim_id, numero_venda, produto, status, ml_envio FROM devolucoes "
                     "WHERE status NOT IN ('finalizada') AND ml_claim_id IS NOT NULL AND ml_review IS NULL ORDER BY id").fetchall()
n = 0
for r in rows:
    if r["ml_envio"] != "delivered":
        continue
    st, cl = req(f"/post-purchase/v1/claims/{r['ml_claim_id']}")
    ac = d._acoes_seller(cl if isinstance(cl, dict) else {})
    if "return_review_ok" in ac or "return_review_fail" in ac:
        n += 1
        print(f"   #{r['id']:<4} {r['status']:<11} venda {r['numero_venda']}  {str(r['produto'] or '')[:55]}", flush=True)
    time.sleep(0.4)
print(f"   total: {n}")
