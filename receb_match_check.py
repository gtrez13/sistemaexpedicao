#!/usr/bin/env python3
"""
TESTE (só leitura): as notas dos fornecedores trazem o nº do pedido (xPed)? os cProd batem com o pedido de compra do Bling?

    venv/bin/python receb_match_check.py                 # notas de entrada dos últimos 90 dias que o Bling tem (XML pelo link)
    venv/bin/python receb_match_check.py /tmp/xmls/      # + todos os .xml de uma pasta (baixados da aba "Notas recebidas")
"""
from __future__ import annotations

import glob
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

from services.bling_service import BlingService

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}
MEU_CNPJ = re.sub(r"\D", "", os.environ.get("EMPRESA_CNPJ", "37672907000107"))
b = BlingService()


def g(el, p):
    return (el.findtext(p, default="", namespaces=NS) or "").strip() if el is not None else ""


def analisar(xml_txt: str, origem: str, pcs_cache: dict):
    try:
        root = ET.fromstring(xml_txt.encode("utf-8"))
    except Exception as e:
        print(f"  [{origem}] XML inválido: {e}"); return
    inf = root.find(".//n:infNFe", NS)
    if inf is None:
        print(f"  [{origem}] sem infNFe"); return
    emit = inf.find("n:emit", NS)
    cnpj = re.sub(r"\D", "", g(emit, "n:CNPJ"))
    if cnpj == MEU_CNPJ:
        print(f"  [{origem}] emitente = LHVMED (nota própria) — pulada"); return   # nota nossa (devolução/entrada própria), não interessa
    nome = g(emit, "n:xNome")[:35]
    nnf = g(inf.find("n:ide", NS), "n:nNF")
    infcpl = g(inf.find("n:infAdic", NS), "n:infCpl")
    ped_txt = re.findall(r"pedido\D{0,15}(\d{3,7})", infcpl, flags=re.I)
    itens = inf.findall("n:det", NS)
    xpeds = sorted({g(d.find("n:prod", NS), "n:xPed") for d in itens} - {""})
    cprods = [g(d.find("n:prod", NS), "n:cProd") for d in itens]
    eans = [g(d.find("n:prod", NS), "n:cEAN") for d in itens]

    # pedidos de compra abertos do fornecedor
    if cnpj not in pcs_cache:
        pcs = []
        try:
            ct = b._get_json("/contatos", {"numeroDocumento": cnpj, "limite": 5}).get("data") or []
            for fid in [x["id"] for x in ct if re.sub(r"\D", "", x.get("numeroDocumento") or "") == cnpj][:2]:
                for sit in (0, 1, 3):
                    for pc in b._get_json("/pedidos/compras", {"idFornecedor": fid, "idsSituacoes[]": sit, "limite": 30}).get("data") or []:
                        det = (b._get_json(f"/pedidos/compras/{pc['id']}") or {}).get("data") or {}
                        pcs.append({"numero": pc.get("numero"), "sit": sit, "cods": {str(i.get("codigoFornecedor") or "").upper() for i in det.get("itens") or []},
                                    "skus": {str((i.get("produto") or {}).get("codigo") or "").upper() for i in det.get("itens") or []}})
                        time.sleep(0.35)
        except Exception as e:
            print(f"  (Bling pedidos de compra: {e})")
        pcs_cache[cnpj] = pcs
    pcs = pcs_cache[cnpj]
    bate = []
    for pc in pcs:
        n = sum(1 for c in cprods if c.upper() in pc["cods"])
        n2 = sum(1 for c in cprods if c.upper() in pc["skus"])
        if n or n2:
            bate.append(f"PC {pc['numero']} (sit {pc['sit']}): {max(n, n2)}/{len(cprods)} itens por {'codigoFornecedor' if n >= n2 else 'SKU'}")
    print(f"\n  [{origem}] NF {nnf} — {nome} — {len(itens)} itens")
    print(f"     xPed: {xpeds or 'NÃO'}   |  'pedido' no texto adicional: {ped_txt or 'NÃO'}")
    print(f"     cProd: {cprods[:4]}{' …' if len(cprods) > 4 else ''}   EAN: {[e for e in eans if e and e != 'SEM GTIN'][:3] or 'nenhum'}")
    print(f"     pedidos de compra desse fornecedor no Bling: {len(pcs)}   match: {bate or 'NENHUM item bateu'}")


def main():
    pcs_cache = {}
    pasta = sys.argv[1] if len(sys.argv) > 1 else None
    if pasta:
        arqs = glob.glob(os.path.join(pasta, "**", "*.xml"), recursive=True)
        print(f">> {len(arqs)} XML(s) na pasta {pasta}")
        for a in arqs:
            analisar(open(a, encoding="utf-8", errors="ignore").read(), os.path.basename(a), pcs_cache)

    ini = (date.today() - timedelta(days=90)).isoformat()
    print(f"\n>> notas de ENTRADA no Bling desde {ini} (baixando XML pelo link)…")
    for pagina in (1, 2, 3):
        lst = b._get_json("/nfe", {"tipo": 0, "dataEmissaoInicial": ini, "dataEmissaoFinal": date.today().isoformat(), "limite": 50, "pagina": pagina}).get("data") or []
        print(f"   página {pagina}: {len(lst)} nota(s)", flush=True)
        if not lst:
            break
        for n in lst:
            try:
                det = (b._get_json(f"/nfe/{n['id']}") or {}).get("data") or {}
                link = det.get("xml")
                if not link:
                    print(f"  [bling #{det.get('numero')}] sem link de XML — situação {det.get('situacao')} contato {(det.get('contato') or {}).get('nome')}"); continue
                xml = b._download(link).decode("utf-8", "ignore")
                analisar(xml, f"bling #{det.get('numero')}", pcs_cache)
                time.sleep(0.4)
            except Exception as e:
                print(f"  (nota {n.get('numero')}: {e})")
    print("\n>> FIM — manda tudo isso.")


if __name__ == "__main__":
    main()
