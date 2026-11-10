#!/usr/bin/env python3
"""
Marca como FINALIZADA as devoluções que o WMS mostra como em aberto (a caminho / entregue / armazém do ML),
mas cuja reclamação JÁ ESTÁ ENCERRADA no Mercado Livre (foram tratadas fora do WMS, antes do módulo existir).

    venv/bin/python devolucoes_limpar_encerradas.py           # SÓ MOSTRA o que faria (não grava nada)
    venv/bin/python devolucoes_limpar_encerradas.py --gravar  # grava

Reversível: grava na observação "[encerrada no ML antes do WMS]" e o status anterior; para desfazer:
    venv/bin/python devolucoes_limpar_encerradas.py --desfazer
"""
import sys, time, re
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import devolucoes_routes as d
from db import db_session

MARCA = "[encerrada no ML antes do WMS; status anterior: {}]"
gravar, desfazer = "--gravar" in sys.argv, "--desfazer" in sys.argv
em_transito = "--reabrir-em-transito" in sys.argv
pend_rev = "--reabrir-pendente-revisao" in sys.argv

if pend_rev:   # volta as que o script finalizou mas o ML ainda espera a REVISÃO do vendedor
    with db_session() as c:
        rows = c.execute("SELECT id, ml_claim_id, observacoes, produto FROM devolucoes WHERE observacoes LIKE ?", ("%[encerrada no ML antes do WMS%",)).fetchall()
    n = 0
    for r in rows:
        st, cl = d._ml_req("GET", f"/post-purchase/v1/claims/{r['ml_claim_id']}")
        acoes = d._acoes_seller(cl if isinstance(cl, dict) else {})
        if st == 200 and ("return_review_ok" in acoes or "return_review_fail" in acoes):
            m = re.search(r"status anterior: (\w+)\]", r["observacoes"] or "")
            obs = re.sub(r"\s*\[encerrada no ML antes do WMS; status anterior: \w+\]", "", r["observacoes"] or "")
            with db_session() as c:
                c.execute("UPDATE devolucoes SET status=?, observacoes=?, finalizada_em=NULL, finalizada_por=NULL WHERE id=?",
                          (m.group(1) if m else "esperada", obs, r["id"]))
            n += 1; print(f"  reaberta #{r['id']} {str(r['produto'] or '')[:50]} (ML espera a revisão)")
        elif st != 200:
            print(f"  #{r['id']}: ML {st} (tente de novo em 1 min)")
        time.sleep(0.5)
    print(f"{n} reaberta(s)"); sys.exit()

with db_session() as c:
    if desfazer or em_transito:
        n = 0
        filtro = " AND COALESCE(ml_envio,'') NOT IN (?,?,?)" if em_transito else ""
        for r in c.execute("SELECT id, observacoes FROM devolucoes WHERE observacoes LIKE ?" + filtro,
                           ("%[encerrada no ML antes do WMS%",) + (("delivered", "cancelled", "") if em_transito else ())).fetchall():
            m = re.search(r"status anterior: (\w+)\]", r["observacoes"] or "")
            obs = re.sub(r"\s*\[encerrada no ML antes do WMS; status anterior: \w+\]", "", r["observacoes"] or "")
            c.execute("UPDATE devolucoes SET status=?, observacoes=?, finalizada_em=NULL, finalizada_por=NULL WHERE id=?",
                      (m.group(1) if m else "esperada", obs, r["id"])); n += 1
        print(f"desfeito em {n} devolução(ões)"); sys.exit()
    rows = c.execute("SELECT id, ml_claim_id, numero_venda, produto, status, ml_envio, observacoes FROM devolucoes "
                     "WHERE status IN ('esperada','armazem_ml') AND ml_claim_id IS NOT NULL ORDER BY id").fetchall()

print(f"{len(rows)} devoluções abertas no WMS com reclamação do ML. Consultando o ML…\n")
fechar = []
for r in rows:
    st, cl = d._ml_req("GET", f"/post-purchase/v1/claims/{r['ml_claim_id']}")
    est = (cl or {}).get("status") if isinstance(cl, dict) else None
    estagio = (cl or {}).get("stage") if isinstance(cl, dict) else None
    marca = "ENCERRADA no ML -> finalizar" if est == "closed" else "aberta no ML (mantém)"
    print(f"  #{r['id']:<5} venda {r['numero_venda']:<18} {str(r['produto'] or '')[:38]:<38} WMS={r['status']:<10} envio={r['ml_envio'] or '-':<14} ML={est}/{estagio}  {marca if st == 200 else f'(ML {st})'}")
    if st == 200 and est == "closed":
        fechar.append(r)
    time.sleep(0.4)

print(f"\n{len(fechar)} serão marcadas como finalizadas.")
if not gravar:
    print("Nada gravado. Para gravar: venv/bin/python devolucoes_limpar_encerradas.py --gravar"); sys.exit()
agora = int(time.time())
with db_session() as c:
    for r in fechar:
        c.execute("UPDATE devolucoes SET status='finalizada', finalizada_em=?, finalizada_por='ML (encerrada antes do WMS)', "
                  "observacoes=COALESCE(observacoes,'') || ' ' || ?, atualizado_em=? WHERE id=?",
                  (agora, MARCA.format(r["status"]), agora, r["id"]))
print("Gravado. Para desfazer: venv/bin/python devolucoes_limpar_encerradas.py --desfazer")
