"""Mostra os campos obrigatórios da NF do pedido no Bling e marca o que está FALTANDO (Bling: "informações obrigatórias faltando").
Uso: venv/bin/python -u ver_nf.py <ml_id>   — só leitura"""
import sys

try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
from services.bling_service import BlingService

b = BlingService()
pv = b._get_json("/pedidos/vendas", params={"numerosLojas[]": sys.argv[1], "limite": 5}).get("data") or []
if not pv:
    sys.exit("PV não encontrado")
det = b._get_json(f"/pedidos/vendas/{pv[0]['id']}").get("data") or {}
nf_id = (det.get("notaFiscal") or {}).get("id")
nf = b._get_json(f"/nfe/{nf_id}").get("data") or {} if nf_id else {}
falta = []


def campo(rotulo, v, obrig=True):
    vazio = v in (None, "", 0, "0") or (isinstance(v, str) and not v.strip())
    if vazio and obrig:
        falta.append(rotulo)
    print(f"   {'FALTANDO ' if vazio and obrig else '         '}{rotulo}: {v if not vazio else '-'}")


print(f"NF {nf.get('numero')} série {nf.get('serie')} | situação {nf.get('situacao')} | natureza {(nf.get('naturezaOperacao') or {}).get('id')}")
c = nf.get("contato") or {}
e = c.get("endereco") or {}
print("Destinatário:")
campo("nome", c.get("nome"))
campo("CPF/CNPJ", c.get("numeroDocumento"))
campo("endereço", e.get("endereco"))
campo("número", e.get("numero"))
campo("bairro", e.get("bairro"))
campo("CEP", e.get("cep"))
campo("município", e.get("municipio"))
campo("UF", e.get("uf"))
print("Itens:")
for it in nf.get("itens") or []:
    print(f" - {it.get('codigo')} {it.get('descricao')}")
    campo("  NCM (classificação fiscal)", it.get("classificacaoFiscal"))
    campo("  CFOP", it.get("cfop"))
    campo("  unidade", it.get("unidade"))
    campo("  origem", it.get("origem"), obrig=False)
    campo("  valor", it.get("valor"))
    campo("  GTIN/EAN", it.get("gtin"), obrig=False)
print("\nFALTANDO: " + (", ".join(falta) if falta else "nada óbvio — abra a NF no Bling e clique em Enviar para ver o campo em vermelho"))
