#!/usr/bin/env python3
"""
TESTE (só leitura) — o que a API do Bling devolve para RECEBIMENTO:
  1) notas de ENTRADA / recebidas (/nfe tipo=0) — vem a chave? os itens? as "recebidas" aparecem?
  2) pedidos de COMPRA em aberto (/pedidos/compras) — itens, fornecedor, quantidades
  3) detalhe de uma nota de entrada (itens + lote/validade se vier)

    venv/bin/python recebimento_teste_bling.py            # últimos 30 dias
    venv/bin/python recebimento_teste_bling.py 90         # últimos 90 dias
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

from services.bling_service import BlingService

dias = int(sys.argv[1]) if len(sys.argv) > 1 else 30
b = BlingService()
ini = (date.today() - timedelta(days=dias)).isoformat()
fim = date.today().isoformat()


def tenta(titulo, path, params):
    print(f"\n=== {titulo}  GET {path} {params}")
    try:
        r = b._get(path, params=params)
        print(f"HTTP {r.status_code}")
        if r.status_code != 200:
            print(r.text[:400]); return None
        j = r.json()
        data = j.get("data") if isinstance(j, dict) else j
        if isinstance(data, list):
            print(f"{len(data)} registros; primeiro:")
            if data:
                print(json.dumps(data[0], ensure_ascii=False, indent=1)[:1800])
        else:
            print(json.dumps(j, ensure_ascii=False, indent=1)[:1800])
        time.sleep(0.5)
        return data
    except Exception as e:
        print("erro:", e); return None


# 1) notas de entrada (tipo 0) — todas as situações
ent = tenta("NF-e ENTRADA (tipo=0)", "/nfe", {"tipo": 0, "dataEmissaoInicial": ini, "dataEmissaoFinal": fim, "limite": 20})
# situações: 1 Pendente, 2 Cancelada, 3 Emitida/Autorizada, 4 Enviada, 5 Rejeitada, 6 Autorizada, 7 Emitida DANFE, 8 Registrada, 9 Enviada Aguardando, 10 Denegada, 11 Consulta, 12 Bloqueada
for sit in (1, 3, 5, 6, 8, 11, 12):
    d = tenta(f"NF-e ENTRADA situacao={sit}", "/nfe", {"tipo": 0, "situacao": sit, "dataEmissaoInicial": ini, "dataEmissaoFinal": fim, "limite": 5})
    if d:
        print(f"   -> situacao {sit}: {len(d)} nota(s). chaves: {[x.get('chaveAcesso') for x in d[:3]]}")

# 2) detalhe da primeira nota de entrada (itens, lote/validade?)
if ent:
    nid = ent[0].get("id")
    det = tenta(f"DETALHE NF entrada id={nid}", f"/nfe/{nid}", None)
    if isinstance(det, dict):
        itens = det.get("itens") or []
        print(f"   itens: {len(itens)}")
        for it in itens[:5]:
            print("   -", json.dumps(it, ensure_ascii=False)[:400])

# 3) pedidos de compra
pc = tenta("PEDIDOS DE COMPRA (todos)", "/pedidos/compras", {"dataInicial": ini, "dataFinal": fim, "limite": 20})
# situações compra: 0 Em aberto, 1 Atendido, 2 Cancelado, 3 Em andamento
for sit in (0, 3):
    d = tenta(f"PEDIDOS DE COMPRA situacao={sit}", "/pedidos/compras", {"idsSituacoes[]": sit, "dataInicial": ini, "dataFinal": fim, "limite": 5})
if pc:
    pid = pc[0].get("id")
    det = tenta(f"DETALHE PEDIDO COMPRA id={pid}", f"/pedidos/compras/{pid}", None)
    if isinstance(det, dict):
        for it in (det.get("itens") or [])[:5]:
            print("   -", json.dumps(it, ensure_ascii=False)[:400])

print("\n>> FIM. Me manda tudo isso.")
