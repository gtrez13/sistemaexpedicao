#!/usr/bin/env python3
"""Só leitura. O que falta para o módulo de devoluções rodar nesta empresa (naturezas, depósitos, variáveis do .env).
    venv/bin/python sonda_devolucao.py
Roda de dentro da pasta do sistema (lê o .env dela). Não grava nada no Bling nem no banco.
"""
import os, re, sys, unicodedata
from dotenv import load_dotenv
load_dotenv(".env")
sys.path.insert(0, os.getcwd())
from services.bling_service import BlingService

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()

env = lambda k, d="": os.environ.get(k, d).strip()
b = BlingService()
g = lambda p, **q: (b._get_json(p, q or None) or {}).get("data") or []

print("== .env desta empresa")
for k, d in (("DEVOLUCAO_EMPRESA_PADRAO", "LHVMED"), ("BLING_DEPOSITO_LOCAL", "Local"), ("BLING_DEPOSITO_DEFEITOS", "Defeitos / Garantia"),
             ("BLING_NATUREZA_DEV_NC_SP", "15101723921"), ("BLING_NATUREZA_DEV_NC_FORA", "15101723906"),
             ("BLING_NATUREZA_DEVOLUCAO", "Devolução de mercadorias"), ("NF_DEV_ENVIAR_AUTO", "1"),
             ("NF_DEV_EMAIL_PADRAO", ""), ("NF_DEV_TELEFONE_PADRAO", ""), ("ML_SELLER_ID", ""), ("SAC_API_URL", "")):
    v = env(k)
    print(f"  {k:28} = {v or '(vazio)'}{'' if v else f'   -> usa o padrão: {d or '(nada)'}'}")

print("\n== depósitos no Bling")
deps = g("/depositos", limite=100)
for d in deps:
    print(f"  id {d.get('id')}  '{d.get('descricao')}'  {'padrão' if d.get('padrao') else ''}  {'DESATIVADO' if d.get('situacao') == 0 else ''}")
for k, d in (("BLING_DEPOSITO_LOCAL", "Local"), ("BLING_DEPOSITO_DEFEITOS", "Defeitos / Garantia")):
    alvo = env(k) or d
    ok = [x for x in deps if norm(x.get("descricao")) == norm(alvo) or str(x.get("id")) == alvo]
    print(f"  {'OK ' if ok else 'FALTA'} {k}='{alvo}'" + (f" -> id {ok[0]['id']}" if ok else "  (nenhum depósito com esse nome; corrija o nome no .env ou crie no Bling)"))

print("\n== naturezas de operação no Bling (só as que parecem devolução)")
nats = g("/naturezas-operacoes", limite=100)
for n in nats:
    if "devol" in norm(n.get("descricao")):
        print(f"  id {n.get('id')}  '{n.get('descricao')}'  situação {n.get('situacao')}")
for k, d in (("BLING_NATUREZA_DEV_NC_SP", "15101723921"), ("BLING_NATUREZA_DEV_NC_FORA", "15101723906"), ("BLING_NATUREZA_DEVOLUCAO", "Devolução de mercadorias")):
    alvo = env(k) or d
    ok = [x for x in nats if str(x.get("id")) == alvo or norm(x.get("descricao")) == norm(alvo)]
    print(f"  {'OK ' if ok else 'FALTA'} {k}='{alvo}'" + (f" -> id {ok[0]['id']} '{ok[0]['descricao']}'" if ok else
          "  (não existe nesta empresa: crie a natureza no Bling e ponha o id ou o nome exato no .env)"))
print("\nDica: a API não mostra o CFOP da regra de ICMS; confira na tela da natureza que está x202 (devolução de venda), não x102.")
