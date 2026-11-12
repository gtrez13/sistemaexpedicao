"""SÓ LEITURA. Procura no Bling a nota do FORNECEDOR que veio sozinha da SEFAZ (sem ninguém dar entrada),
por todos os caminhos da API: chave de acesso, número, período de emissão (varrendo as páginas) e rotas de 'notas recebidas'.
Uso: venv/bin/python -u achar_nota_bling.py <nº da NF ou chave de 44 dígitos>"""
import json
import re
import sys
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import recebimento_routes as rr
from db import db_session

arg = re.sub(r"\D", "", sys.argv[1] if len(sys.argv) > 1 else "")
if not arg:
    sys.exit("Passe o nº da NF ou a chave de 44 dígitos.")
rr.garantir_tabelas()


def raw(metodo, caminho, params=None):
    for k in range(4):
        st, txt, js = rr._bling_raw(metodo, caminho, params=params)
        if st != 429:
            return st, txt, js
        time.sleep(2 + k)
    return st, txt, js


with db_session() as c:
    r = (c.execute("SELECT * FROM receb_notas WHERE chave=?", (arg,)).fetchone() if len(arg) == 44 else
         c.execute("SELECT * FROM receb_notas WHERE numero=? OR numero=? ORDER BY id DESC", (arg.lstrip("0"), arg)).fetchone())
chave = r["chave"] if r else (arg if len(arg) == 44 else "")
numero = (r["numero"] if r else arg.lstrip("0")) or ""
emissao = str(r["emissao"] or "")[:10] if r else ""
print(f"Procurando NF {numero} · chave {chave or '?'} · emissão {emissao or '?'}\n")
achadas = {}


def guarda(fonte, lst):
    n = 0
    for x in lst or []:
        ch = re.sub(r"\D", "", str(x.get("chaveAcesso") or ""))
        if (chave and ch == chave) or (not chave and str(x.get("numero") or "").lstrip("0") == numero):
            achadas.setdefault(x.get("id"), (fonte, x))
            n += 1
    return n


# 1) filtros diretos
for params in ({"chaveAcesso": chave}, {"tipo": 0, "chaveAcesso": chave}, {"tipo": 0, "numero": numero}, {"numero": numero},
               {"tipo": 0, "numero": numero, "serie": r["serie"] if r else None}):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    if not params or (("chaveAcesso" in params) and not chave):
        continue
    st, txt, js = raw("GET", "/nfe", {**params, "limite": 100})
    lst = (js or {}).get("data") or []
    print(f"GET /nfe {params} -> HTTP {st} · {len(lst)} nota(s) · bate: {guarda(f'/nfe {params}', lst)}")
    if st != 200:
        print(f"     {txt[:200]}")

# 2) varre as notas de ENTRADA do período da emissão (±3 dias) e, sem data, as 10 primeiras páginas
if emissao:
    import datetime as dtm
    d0 = dtm.date.fromisoformat(emissao)
    ini, fim = (d0 - dtm.timedelta(days=3)).isoformat(), (d0 + dtm.timedelta(days=3)).isoformat()
    tot = 0
    for pag in range(1, 30):
        st, txt, js = raw("GET", "/nfe", {"tipo": 0, "dataEmissaoInicial": f"{ini} 00:00:00", "dataEmissaoFinal": f"{fim} 23:59:59",
                                          "pagina": pag, "limite": 100})
        lst = (js or {}).get("data") or []
        tot += len(lst)
        guarda(f"/nfe tipo 0 emissão {ini}..{fim}", lst)
        if len(lst) < 100:
            break
    print(f"GET /nfe tipo 0 emitidas de {ini} a {fim}: {tot} nota(s) de entrada no período")
tot = 0
for pag in range(1, 11):
    st, txt, js = raw("GET", "/nfe", {"tipo": 0, "pagina": pag, "limite": 100})
    lst = (js or {}).get("data") or []
    tot += len(lst)
    guarda("/nfe tipo 0 (mais recentes)", lst)
    if len(lst) < 100:
        break
print(f"GET /nfe tipo 0 (10 páginas mais recentes): {tot} nota(s)")

# 3) rotas de 'notas recebidas / destinadas / DF-e' (GET não grava nada)
st_c, txt_c, _ = raw("GET", "/rota-que-nao-existe-wms-teste")
print(f"\nRotas de notas recebidas (rota inventada responde {st_c}):")
for cam in ("/nfe/recebidas", "/nfe/destinadas", "/nfe/entradas", "/nfe/dfe", "/dfe", "/notas-recebidas", "/notas-fiscais/entrada",
            "/notas-fiscais/recebidas", "/nfe/terceiros", "/manifestacoes", "/nfe/manifestacoes", "/documentos-fiscais"):
    st, txt, js = raw("GET", cam, {"limite": 5})
    marca = "IGUAL à inventada (não existe)" if (st, txt[:120]) == (st_c, txt_c[:120]) else "DIFERENTE (!)"
    print(f"   GET {cam:28} -> {st} {marca} {'' if marca.startswith('IGUAL') else txt[:200]}")

print("\n== RESULTADO ==")
if not achadas:
    print("   NÃO achei essa nota pela API.")
for nid, (fonte, x) in achadas.items():
    st, txt, js = raw("GET", f"/nfe/{nid}")
    d = (js or {}).get("data") or {}
    sit = d.get("situacao")
    print(f"   ACHEI: id {nid} (por {fonte})")
    print(f"     tipo {d.get('tipo')} · situação {sit} · nº {d.get('numero')}/{d.get('serie')} · emissão {d.get('dataEmissao')} · "
          f"contato {(d.get('contato') or {}).get('nome')} · natureza {(d.get('naturezaOperacao') or {}).get('id')}")
    print(f"     itens {len(d.get('itens') or [])} · parcelas {len(d.get('parcelas') or [])} · xml {'sim' if d.get('xml') else 'não'}")
    for it in (d.get("itens") or [])[:12]:
        print(f"       - {str(it.get('codigo')):18} {str(it.get('descricao'))[:45]:45} qtd {it.get('quantidade')} · valor {it.get('valor')}"
              f"{' · pedido ' + str(it.get('numeroPedidoCompra')) if it.get('numeroPedidoCompra') else ''}")
    extras = {k: v for k, v in d.items() if k not in ("itens", "parcelas", "contato", "transporte", "xml", "linkDanfe", "linkPDF")}
    print("     campos: " + json.dumps(extras, ensure_ascii=False)[:900])
print("\nNada foi criado nem alterado.")
