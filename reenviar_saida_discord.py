"""Reenvia no Discord um fechamento da Saída de HOJE com os números conferidos no servidor (CORREÇÃO).
Sem 'enviar' só mostra o que vai mandar (não manda nada, não grava nada).
Uso:
  venv/bin/python -u reenviar_saida_discord.py FLEX            # prévia do último fechamento FLEX de hoje
  venv/bin/python -u reenviar_saida_discord.py FLEX enviar     # manda a correção no Discord
  venv/bin/python -u reenviar_saida_discord.py 5 enviar        # fechamento #5
"""
import calendar
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
import requests
import mapa_routes as m
from db import db_session

if not hasattr(m, "_bipados_hoje"):
    sys.exit("Instale antes o backend novo da Saída (atualizar_mapa_foto.sh ef9e57e ou mais novo).")
args = [a.upper() for a in sys.argv[1:]]
enviar = "ENVIAR" in args
alvo = next((a for a in args if a != "ENVIAR"), "FLEX")
hoje = m._hoje()
ini = calendar.timegm(time.strptime(hoje, "%Y-%m-%d")) + 3 * 3600
hm = lambda t: time.strftime("%H:%M", time.gmtime(int(t or 0) - 3 * 3600))

with db_session() as c:
    m._saida_tabelas(c)
    m._fech_tabela(c)
    if alvo.lstrip("#").isdigit():
        f = c.execute("SELECT * FROM saida_fechamentos WHERE id=?", (int(alvo.lstrip("#")),)).fetchone()
    else:
        f = c.execute("SELECT * FROM saida_fechamentos WHERE carro=? AND em >= ? ORDER BY id DESC", (alvo, ini)).fetchone()
    if not f:
        sys.exit(f"Nenhum fechamento {alvo} hoje.")
    f = dict(f)
    carro = f["carro"]
    ids = m._bipados_hoje(c, carro)
    nomes, quem = {}, {}
    for mid in ids:
        r = c.execute("SELECT cliente_nome FROM pedidos WHERE ml_id=?", (mid,)).fetchone()
        lg = c.execute("SELECT cliente FROM saida_log WHERE ml_id=? AND acao IN ('carregou','repetido') ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
        nomes[mid] = (r["cliente_nome"] if r else None) or (lg["cliente"] if lg else None) or "?"
        b = c.execute("SELECT por, em FROM saida_bipes WHERE ml_id=?", (mid,)).fetchone()
        q = (b["por"] if b else None) or "?"
        quem[q] = quem.get(q, 0) + 1
    bips = {r["ml_id"] for r in c.execute("SELECT ml_id FROM saida_bipes WHERE em >= ?", (ini,)).fetchall()}
    wms_ids = {}
    for r in c.execute("SELECT ml_id, ml_shipping_id FROM pedidos WHERE ml_shipping_id IS NOT NULL AND ml_shipping_id <> ''").fetchall():
        wms_ids[str(r["ml_shipping_id"])] = str(r["ml_id"])
faltando_antes = [x for x in (f["faltando"] or "").split(",") if x]

# o que o Mercado Livre ainda tem desse carro (pronto para enviar) e não foi bipado
pend, outro, sem = [], 0, 0
if carro in ("FLEX", "COLETA"):
    print("Lendo os envios no Mercado Livre…")
    envios = m._montar_envios_ml()
    for e in envios.values():
        if e["tipo"] != carro:
            continue
        chaves = {e["chave"], e["pack"], e["sid"], *e["orders"], wms_ids.get(e["sid"], "")} - {""}
        if chaves & bips:
            continue
        lim = e.get("lim")
        if not lim:
            sem += 1
        elif lim > hoje:
            outro += 1
        else:
            pend.append(e)

total = len(ids) + len(faltando_antes)
linhas_carr = [f"• #{mid} — {nomes[mid]}" for mid in ids[:25]] + ([f"… e mais {len(ids) - 25}"] if len(ids) > 25 else [])
linhas_pend = [f"• #{e['chave']} — {e['cliente'] or '?'}{' (sem NF)' if e.get('sub') == 'invoice_pending' else ''}"
               f"{' (atrasado)' if e.get('lim') and e['lim'] < hoje else ''}" for e in pend[:20]] + ([f"… e mais {len(pend) - 20}"] if len(pend) > 20 else [])
nome = m._NOME_CARRO.get(carro, carro)
antes = f"{f['carregados']} de {f['total']}"
if ids:
    titulo = f"CORREÇÃO: carro {nome} — {len(ids)} pacote(s) bipado(s) hoje" + (f", {len(faltando_antes)} faltando" if faltando_antes else "")
    cor = 0xE53935 if faltando_antes else 0x22C55E
else:
    titulo = f"CORREÇÃO: carro {nome} — NENHUM pacote bipado hoje na Saída"
    cor = 0xF59E0B
campos = [
    {"name": "Fechamento", "value": f"#{f['id']} às {hm(f['em'])} por {f['por'] or '?'}", "inline": True},
    {"name": "Mensagem anterior dizia", "value": antes, "inline": True},
    {"name": "Bipados hoje neste carro", "value": str(len(ids)) + (f" de {total}" if faltando_antes else ""), "inline": True},
]
if linhas_carr:
    campos.append({"name": "Pacotes bipados", "value": "\n".join(linhas_carr)[:1000]})
if quem:
    campos.append({"name": "Bipado por", "value": ", ".join(f"{q} ({n})" for q, n in sorted(quem.items(), key=lambda x: -x[1]))[:1000]})
if faltando_antes:
    campos.append({"name": "Faltando (registrado no fechamento)", "value": m._lista_pedidos(faltando_antes)[:1000] or "-"})
if carro in ("FLEX", "COLETA"):
    campos.append({"name": f"Ainda no Mercado Livre para sair hoje, sem bipe ({len(pend)})",
                   "value": ("\n".join(linhas_pend) or "nenhum")[:1000]})
    campos.append({"name": "Mercado Livre: outros", "value": f"de outro dia: {outro} · sem data no ML: {sem}"})
embed = {"title": titulo, "color": cor, "fields": campos,
         "description": (f"A mensagem das {hm(f['em'])} saiu com números errados. Estes são os números conferidos no servidor." if ids else
                         f"A mensagem das {hm(f['em'])} saiu como 'tudo carregado', mas nenhum pacote desse carro foi bipado na Saída hoje."),
         "footer": {"text": f"NexusWMS LHVMed · Saída · correção do fechamento #{f['id']}"},
         "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
payload = {"content": f"Correção da saída {nome} de hoje", "embeds": [embed], "allowed_mentions": {"parse": []}}

print(f"\n== PRÉVIA ==\n{payload['content']}\n{titulo}\n{embed['description']}")
for x in campos:
    print(f"-- {x['name']}:\n   " + x["value"].replace("\n", "\n   "))
if not enviar:
    print("\nNada foi enviado. Se estiver certo, rode de novo com 'enviar' no final.")
    sys.exit(0)
url = os.environ.get("DISCORD_WEBHOOK_SAIDA", "").strip()
if not url:
    sys.exit("DISCORD_WEBHOOK_SAIDA não está no .env")
r = requests.post(url, json=payload, timeout=20)
print(f"\nDiscord: HTTP {r.status_code} {r.text[:200]}")
if r.status_code in (200, 204):
    with db_session() as c:
        if len(ids) > (f["carregados"] or 0):
            c.execute("UPDATE saida_fechamentos SET carregados=?, total=? WHERE id=?", (len(ids), max(total, f["total"] or 0), f["id"]))
        c.execute("INSERT INTO saida_log (ml_id, cliente, acao, codigo, tipo, carro, por, em, detalhe) VALUES (?,?,?,?,?,?,?,?,?)",
                  (None, None, "corrigiu_discord", None, carro, carro, "sistema", int(time.time()),
                   f"fechamento #{f['id']}: antes {antes}, corrigido {len(ids)} bipados" + (f" de {total}" if faltando_antes else "")))
    print("OK — correção enviada e registrada no histórico.")
