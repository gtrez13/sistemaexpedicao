"""SÓ LEITURA. Mostra como o Bling guarda uma NOTA DE DEVOLUÇÃO feita à mão ("Gerar devolução"), para o WMS criar
igualzinho pela API. Também lista depósitos e naturezas de operação.
Uso: venv/bin/python -u sonda_devolucao_bling.py <nº da NF de devolução> [série]"""
import json
import sys

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from services.bling_service import BlingService

b = BlingService()
num = sys.argv[1] if len(sys.argv) > 1 else ""
serie = sys.argv[2] if len(sys.argv) > 2 else None

print("== Depósitos ==")
for d in (b._get_json("/depositos", params={"limite": 100}).get("data") or []):
    print(f"   {d.get('id')}  {d.get('descricao')}  padrão={d.get('padrao')}  situação={d.get('situacao')}")
print("== Naturezas de operação ==")
for n in (b._get_json("/naturezas-operacoes", params={"limite": 100}).get("data") or []):
    print(f"   {n.get('id')}  {n.get('descricao')}  padrão={n.get('padrao')}")

if num:
    achou = None
    if not num.isdigit():
        sys.exit("Passe o NÚMERO da NF de devolução (só dígitos), ex.: 120772")
    for params in ({"numero": num, "tipo": 0}, {"numero": num, "tipo": 1}, {"numero": num}):
        try:
            lst = b._get_json("/nfe", params={**params, "limite": 20}).get("data") or []
        except Exception as e:
            print(f"   (busca {params} recusada: {str(e)[:120]})")
            continue
        for nf in lst:
            if str(nf.get("numero")).lstrip("0") == str(num).lstrip("0") and (not serie or str(nf.get("serie")) == str(serie)):
                achou = nf
                break
        if achou:
            break
    if not achou:
        sys.exit(f"NF {num} não encontrada no Bling")
    det = b._get_json(f"/nfe/{achou['id']}").get("data") or {}
    # tira o que é pessoal/ruído para caber na tela; mantém a estrutura
    det.pop("xml", None); det.pop("linkDanfe", None); det.pop("linkPDF", None)
    print(f"\n== NF {num} (id {achou['id']}) — estrutura completa ==")
    print(json.dumps(det, ensure_ascii=False, indent=1)[:9000])
