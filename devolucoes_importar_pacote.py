#!/usr/bin/env python3
"""
Importa/atualiza UMA devolução direto pelo número que aparece no painel do ML (pacote ou venda). Rápido: ~5 consultas.
    venv/bin/python devolucoes_importar_pacote.py 2000019016928428 2000015576482419
"""
import sys, time, re
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import devolucoes_routes as d
from db import db_session


def req(path, **kw):
    for n in range(8):
        st, j = d._ml_req("GET", path, **kw)
        if st != 429:
            return st, j
        print(f"   (limite do ML: esperando {30 + n * 30} s)", flush=True); time.sleep(30 + n * 30)
    return st, j


d.garantir_tabelas()
for num in [re.sub(r"\D", "", a) for a in sys.argv[1:] if re.sub(r"\D", "", a)]:
    print(f"\n== {num}", flush=True)
    st, pk = req(f"/packs/{num}")
    pedidos = [str(o.get("id")) for o in (pk or {}).get("orders") or []] if st == 200 and isinstance(pk, dict) else [num]
    print(f"   pedidos: {pedidos}")
    for oid in pedidos:
        st, cs = req("/post-purchase/v1/claims/search", params={"resource_id": oid, "resource": "order", "limit": 10})
        claims = (cs or {}).get("data") or [] if st == 200 and isinstance(cs, dict) else []
        if not claims:
            print(f"   pedido {oid}: nenhuma reclamação encontrada (ML {st})"); continue
        for c in claims:
            cid = str(c.get("id"))
            st, ret = req(f"/post-purchase/v2/claims/{cid}/returns")
            if st != 200 or not isinstance(ret, dict):
                print(f"   reclamação {cid}: sem devolução (ML {st})"); continue
            tem, destino, envio = d._destino_da_devolucao(ret)
            status = "armazem_ml" if destino == "warehouse" else "esperada"
            with db_session() as conn:
                ex = conn.execute("SELECT id, status FROM devolucoes WHERE ml_claim_id=?", (cid,)).fetchone()
                if ex:
                    novo = ex["status"] if ex["status"] not in ("esperada", "armazem_ml", "finalizada") else status
                    conn.execute("UPDATE devolucoes SET ml_envio=?, status=?, atualizado_em=? WHERE id=?", (envio, novo, int(time.time()), ex["id"]))
                    print(f"   já estava no WMS (#{ex['id']}): atualizada -> {novo}, envio {envio}, destino {destino}"); continue
            _, ped = req(f"/orders/{oid}")
            _, cl = req(f"/post-purchase/v1/claims/{cid}")
            dd = d._dados_do_pedido(ped or {})
            motivo = d._motivo_texto(lambda p, params=None: req(p, params=params), (cl or {}).get("reason_id") or c.get("reason_id"))
            agora = int(time.time())
            sku = dd["sku"] or ""
            row = {"criado_em": agora, "atualizado_em": agora, "responsavel": "Mercado Livre", "status": status,
                   "data_recebimento": time.strftime("%Y-%m-%d"), "loja": "LHV Med", "cliente": dd["cliente"],
                   "numero_venda": oid, "sku": sku or None, "sku_key": sku[:64].lower() if sku else None, "produto": dd["produto"],
                   "qtd": float(dd["qtd"] or 1), "valor": dd["valor"], "teve_reclamacao": 1, "num_reclamacao": cid,
                   "ml_claim_id": cid, "bipado": 0, "checklist": "{}", "ml_envio": envio,
                   "observacoes": f"Importada do Mercado Livre (pacote {num}). Motivo ML: {motivo}. Envio de volta: {envio or '—'}."}
            with db_session() as conn:
                cols = list(row)
                conn.execute(f"INSERT INTO devolucoes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(row[k] for k in cols))
            print(f"   IMPORTADA: {dd['produto'][:50]} | {dd['cliente']} | destino {destino} | envio {envio} -> {status}")
print("\nPronto. Recarregue a tela de Devoluções.")
