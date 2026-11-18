#!/usr/bin/env python3
"""
DEVOLUÇÕES: confere SÓ LENDO se todas as integrações do módulo estão funcionando (LHVMed).
Não cria nota, não lança estoque, não avisa o ML, não manda nada ao SAC, não muda nenhuma devolução.

    venv/bin/python -u devolucoes_diagnostico.py          confere tudo e testa a devolução conferida mais recente
    venv/bin/python -u devolucoes_diagnostico.py 123      confere tudo e testa a devolução 123

O que confere:
  1. Banco          devoluções por situação; quando o WMS olhou o ML pela última vez
  2. Mercado Livre  token; busca de reclamações (o que traz as devoluções "A caminho"); envio de volta e frete;
                    revisão "chegou bem / com problema" (o que o ML espera de vocês); motivos de falha; dados do comprador
  3. Bling          token; depósitos Local e Defeitos / Garantia; naturezas de operação da devolução; e-mail/telefone padrão;
                    NF de saída da venda; PRÉVIA da nota de devolução (o mesmo teste da tela, sem criar nada);
                    histórico das notas (o que o Bling e a SEFAZ responderam) e nota parada no meio
  4. SAC            endereço configurado e se responde (só um GET, sem enviar O.S.)
  5. Fotos e vídeos pasta, espaço em disco e devolução fechada sem vídeo
"""
import os
import shutil
import sys
import time
import traceback

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

if not os.path.exists("app.py"):
    sys.exit("Rode dentro da pasta do backend (onde fica o app.py).")

import devolucoes_routes as d
from db import db_session

FALHAS, ATENCAO = [], []


def ok(t):
    print(f"  [ OK ]  {t}", flush=True)


def falha(t):
    FALHAS.append(t)
    print(f"  [FALHA] {t}", flush=True)


def atencao(t):
    ATENCAO.append(t)
    print(f"  [ !! ]  {t}", flush=True)


def info(t):
    print(f"          {t}", flush=True)


def titulo(t):
    print(f"\n== {t} ==", flush=True)


def tenta(nome, f):
    try:
        return f()
    except Exception as e:
        falha(f"{nome}: erro no teste ({type(e).__name__}: {str(e)[:200]})")
        if os.environ.get("DIAG_DEBUG"):
            traceback.print_exc()
        return None


def ha(seg):
    if not seg:
        return "nunca"
    s = int(time.time() - int(seg))
    if s < 3600:
        return f"há {s // 60} min"
    if s < 86400:
        return f"há {s // 3600} h"
    return f"há {s // 86400} dia(s)"


def tem(tabela, col="1"):
    """A tabela/coluna existe? (cada teste numa conexão própria: no Postgres um SELECT que falha estraga a transação)"""
    try:
        with db_session() as c2:
            c2.execute(f"SELECT {col} FROM {tabela} LIMIT 1").fetchall()
        return True
    except Exception:
        return False


alvo_id = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
print(f"Diagnóstico das devoluções — {time.strftime('%d/%m/%Y %H:%M')}  (só leitura)")

# ───────────────────────── 1. BANCO ─────────────────────────
titulo("1. Banco do WMS")
alvo = None
with db_session() as c:
    try:
        d.garantir_tabelas(c)
    except TypeError:
        d.garantir_tabelas()
    cs = {x for x in ("bipado", "ml_review", "nf_dev_situacao", "nf_dev_etapa") if tem("devolucoes", x)}
    tot = c.execute("SELECT COUNT(*) AS n FROM devolucoes").fetchone()["n"]
    ok(f"{tot} devoluções no banco")
    for r in c.execute("SELECT status, COUNT(*) AS n FROM devolucoes GROUP BY status ORDER BY n DESC").fetchall():
        info(f"{dict(d.STATUS).get(r['status'], r['status'])}: {r['n']}")
    ult = c.execute("SELECT MAX(criado_em) AS m FROM devolucoes WHERE ml_claim_id IS NOT NULL").fetchone()["m"]
    info(f"última devolução trazida do ML: {ha(ult)}")
    try:
        vis = c.execute("SELECT MAX(atualizado_em) AS m, COUNT(*) AS n FROM devolucoes_ml_vistos").fetchone()
        if vis and vis["m"] and time.time() - int(vis["m"]) > 6 * 3600:
            atencao(f"o WMS não olha as reclamações do ML {ha(vis['m'])} (a busca roda quando alguém abre a tela Devoluções)")
        else:
            ok(f"o WMS olhou as reclamações do ML {ha(vis['m'] if vis else None)}")
    except Exception:
        atencao("tabela devolucoes_ml_vistos não existe: o WMS ainda não buscou devoluções no ML")
    if os.path.exists(d.SYNC_LOCK):
        idade = time.time() - os.path.getmtime(d.SYNC_LOCK)
        (atencao if idade > 3600 else info)(f"devolucoes_sincronizar.py marcado como rodando há {int(idade // 60)} min "
                                            f"(enquanto isso o site não consulta o ML){' — se não está rodando, apague ' + d.SYNC_LOCK if idade > 3600 else ''}")
    if alvo_id:
        alvo = c.execute("SELECT * FROM devolucoes WHERE id=?", (alvo_id,)).fetchone()
        if not alvo:
            falha(f"devolução {alvo_id} não existe")
    if not alvo:
        conf = "AND (bipado=1 OR status NOT IN ('esperada','armazem_ml'))" if "bipado" in cs else "AND status NOT IN ('esperada','armazem_ml')"
        alvo = c.execute(f"SELECT * FROM devolucoes WHERE numero_venda IS NOT NULL AND numero_venda<>'' {conf} "
                         "ORDER BY COALESCE(atualizado_em, criado_em) DESC LIMIT 1").fetchone()
    com_claim = c.execute("SELECT * FROM devolucoes WHERE ml_claim_id IS NOT NULL ORDER BY criado_em DESC LIMIT 1").fetchone()
    sem_revisao = []
    if "ml_review" in cs:
        sem_revisao = c.execute("SELECT id, ml_claim_id, status FROM devolucoes WHERE ml_claim_id IS NOT NULL AND ml_review IS NULL "
                                "AND status NOT IN ('esperada','armazem_ml') ORDER BY id DESC LIMIT 3").fetchall()
        rev = {r["ml_review"]: r["n"] for r in c.execute("SELECT ml_review, COUNT(*) AS n FROM devolucoes WHERE ml_review IS NOT NULL GROUP BY ml_review").fetchall()}
        rev_ult = c.execute("SELECT MAX(ml_review_em) AS m FROM devolucoes").fetchone()["m"]
    full_ex = c.execute("SELECT * FROM devolucoes WHERE status='armazem_ml' AND numero_venda IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
if alvo:
    alvo = dict(alvo)
    info(f"devolução usada nos testes: #{alvo['id']} (venda {alvo.get('numero_venda')}, {dict(d.STATUS).get(alvo.get('status'), alvo.get('status'))})")

# ───────────────────────── 2. MERCADO LIVRE ─────────────────────────
titulo("2. Mercado Livre")
seller = str(os.environ.get("ML_SELLER_ID", "")).strip()


def t_token_ml():
    st, me = d._ml_req("GET", "/users/me")
    if st == 200 and isinstance(me, dict):
        ok(f"token do ML vale: conta {me.get('nickname')} ({me.get('id')})")
        if seller and str(me.get("id")) != seller:
            falha(f"ML_SELLER_ID={seller} no .env é diferente da conta do token ({me.get('id')}): o frete de volta sai errado")
        elif not seller:
            atencao("ML_SELLER_ID não está no .env: o frete de volta pode somar o que não é de vocês")
        return True
    falha(f"token do ML não vale (HTTP {st}): nada do ML funciona até renovar")
    return False


ml_ok = tenta("token do ML", t_token_ml)


def t_busca():
    st, j = d._ml_req("GET", "/post-purchase/v1/claims/search", params={"status": "opened", "limit": 1})
    if st == 200 and isinstance(j, dict):
        ok(f"busca de reclamações abertas responde ({(j.get('paging') or {}).get('total', '?')} abertas no ML)")
    elif st == 429:
        atencao("busca de reclamações: o ML pediu para esperar (limite 429). Rode de novo em alguns minutos")
    else:
        falha(f"busca de reclamações não responde (HTTP {st}): as devoluções 'A caminho' não chegam no WMS")


def t_envio_volta():
    cid = com_claim["ml_claim_id"]
    try:
        valor, leu, st_envio = d._custo_frete_volta(d._ml_get, cid)
    except Exception as e:
        if "Limite" in type(e).__name__:
            atencao("envio de volta: limite do ML (429). Rode de novo em alguns minutos")
            return
        raise
    if st_envio or leu:
        ok(f"envio de volta e frete da reclamação {cid}: envio '{st_envio or '-'}', frete cobrado {('R$ %.2f' % valor) if valor is not None else 'não informado'}")
    else:
        atencao(f"reclamação {cid}: o ML não trouxe envio de volta (pode ser reclamação sem devolução física)")


def t_revisao():
    if "ml_review" not in cs:
        falha("o banco não tem as colunas da revisão do ML (ml_review): a versão instalada não avisa o ML")
        return
    txt = ", ".join(f"{k}: {v}" for k, v in rev.items()) or "nenhuma ainda"
    ok(f"revisões já enviadas ao ML — {txt} (última {ha(rev_ult)})")
    for r in sem_revisao:
        st, cl = d._ml_req("GET", f"/post-purchase/v1/claims/{r['ml_claim_id']}")
        if st != 200:
            st, cl = d._ml_req("GET", f"/marketplace/v2/claims/{r['ml_claim_id']}")
        if st != 200:
            falha(f"devolução #{r['id']}: não consegui ler a reclamação {r['ml_claim_id']} (HTTP {st})")
            continue
        ac = d._acoes_seller(cl)
        if "return_review_ok" in ac or "return_review_fail" in ac:
            atencao(f"devolução #{r['id']} já conferida e o ML ainda espera a revisão de vocês (chegou bem / com problema)")
        else:
            info(f"devolução #{r['id']}: o ML não pede revisão nesta reclamação (ações: {', '.join(ac) or 'nenhuma'})")


def t_motivos():
    cid = (sem_revisao[0]["ml_claim_id"] if sem_revisao else com_claim["ml_claim_id"])
    st, j = d._ml_req("GET", "/post-purchase/v1/returns/reasons", params={"flow": "seller_return_failed", "claim_id": cid})
    if st == 200 and j:
        ok(f"motivos de 'chegou com problema' respondem ({len(j) if isinstance(j, list) else 'ok'})")
    else:
        atencao(f"motivos de 'chegou com problema' não vieram do ML (HTTP {st}): a tela usa a lista padrão SRF2..SRF7")


def t_comprador():
    dados, fonte, aviso = d._cliente_ml(alvo["numero_venda"])
    if aviso:
        falha(f"dados do comprador da venda {alvo['numero_venda']}: {aviso}")
        return
    tem = [k for k in ("numeroDocumento", "endereco", "telefone", "email") if dados.get(k)]
    (ok if "numeroDocumento" in tem else atencao)(f"comprador da venda {alvo['numero_venda']} no ML: veio {', '.join(tem) or 'nada'}"
                                                  + ("" if "numeroDocumento" in tem else " (sem CPF/CNPJ a nota não sai)"))


def t_full():
    inv, item = d._inventory_id(full_ex["numero_venda"], full_ex.get("sku") if hasattr(full_ex, "get") else full_ex["sku"])
    (ok if inv else atencao)(f"Full: devolução #{full_ex['id']} -> anúncio {item or '?'} código do Full {inv or 'não achado'}")


if ml_ok:
    tenta("busca de reclamações", t_busca)
    if com_claim:
        tenta("envio de volta", t_envio_volta)
        tenta("revisão do ML", t_revisao)
        tenta("motivos de falha", t_motivos)
    else:
        atencao("nenhuma devolução com reclamação do ML no banco para testar envio de volta e revisão")
    if alvo:
        tenta("dados do comprador", t_comprador)
    if full_ex:
        tenta("Full", t_full)

# ───────────────────────── 3. BLING ─────────────────────────
titulo("3. Bling (nota de devolução)")
b = None


def t_token_bling():
    global b
    from services.bling_service import BlingService
    b = BlingService()
    deps = b._get_json("/depositos", params={"limite": 100}).get("data") or []
    ok(f"token do Bling vale ({len(deps)} depósitos)")
    nomes = {d._norm_txt(x.get("descricao")): x for x in deps}
    for rot, nome in (("Local", d.DEPOSITO_LOCAL), ("Defeitos / Garantia", d.DEPOSITO_DEFEITOS)):
        x = nomes.get(d._norm_txt(nome)) or (next((y for y in deps if str(y.get("id")) == str(nome)), None) if str(nome).isdigit() else None)
        if x:
            ok(f"depósito '{nome}' existe (id {x.get('id')}{', inativo!' if str(x.get('situacao')) in ('0', 'I') else ''})")
        else:
            falha(f"depósito '{nome}' ({rot}) não existe no Bling: a nota não consegue lançar o estoque. Nomes no Bling: "
                  + ", ".join(sorted(str(y.get('descricao')) for y in deps))[:300])
    return True


bling_ok = tenta("token do Bling", t_token_bling)


def t_naturezas():
    achadas, pag = {}, 1
    while pag <= 6:
        lst = b._get_json("/naturezas-operacoes", params={"pagina": pag, "limite": 100}).get("data") or []
        for x in lst:
            achadas[str(x.get("id"))] = x.get("descricao")
        if len(lst) < 100:
            break
        pag += 1
    for rot, val in (("cliente com IE", d.NATUREZA_DEVOLUCAO), ("CPF/sem IE em SP", d.NATUREZA_DEV_NC_SP), ("CPF/sem IE fora de SP", d.NATUREZA_DEV_NC_FORA)):
        if str(val).isdigit():
            (ok if str(val) in achadas else falha)(f"natureza {rot}: id {val} " + (f"= '{achadas[str(val)]}'" if str(val) in achadas else "NÃO existe no Bling"))
        else:
            hit = [k for k, v in achadas.items() if d._norm_txt(v) == d._norm_txt(val)]
            (ok if hit else atencao)(f"natureza {rot}: '{val}' " + (f"existe (id {hit[0]})" if hit else "não achei pelo nome (só usada se o cliente tiver IE)"))


def t_padroes():
    falt = [n for n, v in (("NF_DEV_EMAIL_PADRAO", d.NF_DEV_EMAIL_PADRAO), ("NF_DEV_TELEFONE_PADRAO", d.NF_DEV_TELEFONE_PADRAO)) if not v]
    if falt:
        falha(f"{' e '.join(falt)} vazio no .env: cliente do ML não tem e-mail/telefone e o Bling recusa a nota")
    else:
        ok("e-mail e telefone padrão configurados (cliente do ML sem esses dados)")
    info(f"envio automático para a SEFAZ: {'LIGADO' if d.ENVIAR_SEFAZ_AUTO() else 'DESLIGADO (NF_DEV_ENVIAR_AUTO=0)'}")


def t_nf_saida():
    nf, aviso, _ = d._venda_bling(alvo["numero_venda"])
    (ok if nf else falha)(f"NF de saída da venda {alvo['numero_venda']} no Bling: {nf or aviso}")


def t_previa():
    p = d._plano_nf_dev(alvo, None)
    sim = {True: "ok", False: "TRAVA", None: "confira"}
    for pt in p.get("pontos") or []:
        info(f"{sim.get(pt.get('ok'), '?'):>7}  {pt.get('item')}{(': ' + str(pt.get('detalhe'))) if pt.get('detalhe') else ''}"[:220])
    dup = p.get("duplicada")
    if dup:
        info(f"já existe nota de devolução para essa venda: nº {str(dup.get('numero') or '').lstrip('0')} ({d.SIT_NF.get(int(dup.get('situacao') or 0), '?')})")
    if p.get("bloqueio"):
        atencao(f"prévia da nota da devolução #{alvo['id']}: travaria em '{p['bloqueio'][:160]}'")
    else:
        ok(f"prévia da nota da devolução #{alvo['id']}: pode gerar (depósito {p.get('dep') or 'o operador escolhe'})")


def t_historico():
    with db_session() as c:
        if not tem("devolucao_nf_log"):
            atencao("tabela devolucao_nf_log não existe: nenhuma nota de devolução foi tentada nesta versão")
            return
        n = c.execute("SELECT COUNT(*) AS n FROM devolucao_nf_log").fetchone()["n"]
        if not n:
            atencao("nenhuma nota de devolução foi tentada ainda (o fluxo Bling + SEFAZ nunca rodou de verdade)")
            return
        for r in c.execute("SELECT acao, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) AS bons, SUM(CASE WHEN ok=1 THEN 0 ELSE 1 END) AS ruins, "
                           "MAX(em) AS m FROM devolucao_nf_log GROUP BY acao ORDER BY MAX(em) DESC").fetchall():
            info(f"{r['acao']}: {r['bons']} ok, {r['ruins']} com erro (último {ha(r['m'])})")
        aut = c.execute("SELECT COUNT(*) AS n FROM devolucao_nf_log WHERE acao='resposta da SEFAZ' AND ok=1").fetchone()["n"]
        (ok if aut else atencao)(f"notas autorizadas pela SEFAZ: {aut}" + ("" if aut else " — nenhuma nota chegou até a SEFAZ ainda"))
        ruins = c.execute("SELECT devolucao_id, acao, http, mensagem, em FROM devolucao_nf_log WHERE ok=0 AND em > ? ORDER BY id DESC LIMIT 6",
                          (int(time.time()) - 15 * 86400,)).fetchall()
        for r in ruins:
            info(f"erro #{r['devolucao_id']} {ha(r['em'])} em '{r['acao']}' (HTTP {r['http']}): {str(r['mensagem'] or '')[:160]}")
        if "nf_dev_situacao" in cs:
            for r in c.execute("SELECT nf_dev_situacao AS s, COUNT(*) AS n FROM devolucoes WHERE nf_dev_id IS NOT NULL GROUP BY nf_dev_situacao").fetchall():
                info(f"notas de devolução no Bling — {r['s'] or 'sem situação'}: {r['n']}")
        if "nf_dev_etapa" in cs:
            presas = c.execute("SELECT id, nf_dev_etapa, nf_dev_etapa_em FROM devolucoes WHERE nf_dev_etapa IS NOT NULL "
                               "AND nf_dev_etapa NOT IN ('ok','erro') AND COALESCE(nf_dev_etapa_em,0) < ?", (int(time.time()) - 900,)).fetchall()
            for r in presas:
                falha(f"devolução #{r['id']}: nota parada em '{getattr(d, 'ETAPA_TXT', {}).get(r['nf_dev_etapa'], r['nf_dev_etapa'])}' "
                      f"desde {ha(r['nf_dev_etapa_em'])} (o serviço reiniciou no meio?) — confira a nota no Bling")
            if not presas:
                ok("nenhuma nota parada no meio do caminho")


if bling_ok:
    tenta("naturezas de operação", t_naturezas)
    tenta("e-mail/telefone padrão", t_padroes)
    if alvo:
        tenta("NF de saída", t_nf_saida)
        tenta("prévia da nota de devolução", t_previa)
tenta("histórico das notas", t_historico)

# ───────────────────────── 4. SAC ─────────────────────────
titulo("4. SAC")


def t_sac():
    base = os.environ.get("SAC_API_URL", "").strip().rstrip("/")
    with db_session() as c:
        st = {r["s"]: r["n"] for r in c.execute("SELECT COALESCE(sac_status,'pendente') AS s, COUNT(*) AS n FROM devolucoes GROUP BY COALESCE(sac_status,'pendente')").fetchall()}
        erros = c.execute("SELECT id, sac_msg FROM devolucoes WHERE sac_status='erro' ORDER BY id DESC LIMIT 3").fetchall()
    info("situação no SAC: " + (", ".join(f"{k}: {v}" for k, v in st.items()) or "nenhuma"))
    if not base:
        atencao("SAC_API_URL não está no .env: o envio ao SAC é SIMULADO (nada sai do WMS)")
        return
    import requests
    try:
        r = requests.get(base, timeout=8)
        (ok if r.status_code < 500 else falha)(f"SAC em {base} responde (HTTP {r.status_code}, só um GET, nenhuma O.S. enviada)")
    except Exception as e:
        falha(f"SAC em {base} não responde: {str(e)[:150]}")
    if not os.environ.get("SAC_API_TOKEN", "").strip():
        atencao("SAC_API_TOKEN vazio: se o SAC pede token, o envio vai dar 401")
    for r in erros:
        info(f"erro no SAC #{r['id']}: {str(r['sac_msg'] or '')[:160]}")


tenta("SAC", t_sac)

# ───────────────────────── 5. FOTOS E VÍDEOS ─────────────────────────
titulo("5. Fotos e vídeos")


def t_fotos():
    pasta = os.environ.get("DEVOLUCOES_FOTOS_DIR") or os.path.join(os.path.dirname(os.path.abspath(d.__file__)), "uploads_devolucoes")
    if not os.path.isdir(pasta):
        atencao(f"pasta das fotos {pasta} ainda não existe (é criada na 1ª foto)")
        return
    livre = shutil.disk_usage(pasta).free / 1e9
    (ok if os.access(pasta, os.W_OK) else falha)(f"pasta {pasta}: {'grava' if os.access(pasta, os.W_OK) else 'SEM permissão de gravar'}, "
                                                f"{len(os.listdir(pasta))} arquivos, {livre:.1f} GB livres")
    if livre < 5:
        atencao(f"só {livre:.1f} GB livres no disco das fotos/vídeos")
    with db_session() as c:
        nf_ = c.execute("SELECT COUNT(*) AS n FROM devolucao_fotos").fetchone()["n"]
        nv = c.execute("SELECT COUNT(*) AS n FROM devolucao_videos").fetchone()["n"] if tem("devolucao_videos") else 0
        sem = c.execute("SELECT COUNT(*) AS n FROM devolucoes x WHERE status='finalizada' AND NOT EXISTS "
                        "(SELECT 1 FROM devolucao_videos v WHERE v.devolucao_id=x.id)").fetchone()["n"] if tem("devolucao_videos") else 0
        perdidas = 0
        for r in c.execute("SELECT arquivo FROM devolucao_fotos ORDER BY id DESC LIMIT 30").fetchall():
            if r["arquivo"] and not os.path.exists(os.path.join(pasta, r["arquivo"])):
                perdidas += 1
    info(f"{nf_} fotos e {nv} vídeos registrados")
    if perdidas:
        falha(f"{perdidas} das 30 últimas fotos estão no banco mas o arquivo sumiu da pasta")
    if sem:
        info(f"{sem} devoluções finalizadas sem vídeo (antes da regra do vídeo de 5 s, provavelmente)")


tenta("fotos e vídeos", t_fotos)

# ───────────────────────── RESUMO ─────────────────────────
print("\n== RESUMO ==")
if not FALHAS and not ATENCAO:
    print("  Tudo funcionando.")
for t in FALHAS:
    print(f"  FALHA: {t}")
for t in ATENCAO:
    print(f"  ATENÇÃO: {t}")
print("\n(Nada foi criado nem mudado. Me mande esta saída inteira.)")
