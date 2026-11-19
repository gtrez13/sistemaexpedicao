"""Tarefas + inventário rotativo (Biel 17/11). LHVMed.

TAREFAS: admin e supervisor passam tarefas para uma pessoa ou para qualquer um; quem recebe vê na Início e num aviso.
INVENTÁRIO ROTATIVO: depois das INV_HORA (14:00, horário de Brasília), de segunda a sexta, o WMS monta a tarefa do dia pelo
mapa do galpão: estruturas inteiras na ordem da rota até completar a cota do dia, que é (posições que faltam contar no mês) /
(dias úteis que sobram no mês), para fechar o galpão todo até o fim do mês. Todo mundo (menos admin) recebe o aviso a cada
15 min; quem tocar em Começar fica com ela.
CONTAGEM ÀS CEGAS: o operador vê o produto e a posição, nunca o número do Bling. Ao enviar, o WMS soma por produto (um SKU em
várias posições entra inteiro na mesma tarefa) e compara com o saldo do depósito da LHVMed no Bling. Bateu = ok. Não bateu:
pede 1 recontagem às cegas; se a recontagem também não bater, sobe a divergência para o admin, que decide: "pôr N no Bling"
(balanço no depósito), manter o Bling, ou mandar recontar. Bate também quando contado + "separado esperando sair" = Bling
(produto que já saiu da prateleira para pedido que ainda não foi expedido).

Registrado no blueprint do mapa (url_prefix /api) por registrar(bp). Rotas:
  GET  /tarefas[?escopo=minhas|todas]        minhas (padrão) ou todas (admin/supervisor)
  POST /tarefas                              {titulo, descricao?, para?, prazo?, local?}   admin/supervisor
  POST /tarefas/<id>/pegar | /soltar | /feita {obs?} | /cancelar | /vista
  GET  /tarefas/aviso                        o que o aviso (pop-up) mostra agora para quem está logado
  GET  /tarefas/pessoas                      quem pode receber tarefa (admin/supervisor)
  GET  /inicio/resumo                        números da tela Início
  GET  /inventario/mes                       plano do mês: estruturas contadas / de hoje / faltam, cota, ritmo
  POST /inventario/gerar                     admin/supervisor: monta a tarefa de hoje agora (sem esperar as 14h)
  GET  /inventario/tarefa/<id>               posições da contagem (sem números do Bling)
  POST /inventario/tarefa/<id>/contar        {porta_palet, posicao, vazia?, itens: [{sku, qtd, achado?}]}
  POST /inventario/tarefa/<id>/enviar        fecha a rodada e confere com o Bling (em segundo plano)
  GET  /inventario/divergencias[?status=]    admin/supervisor
  POST /inventario/divergencias/<id>/decidir {acao: substituir|manter|recontar|reler, valor?}   admin (reler: supervisor também)

INVENTÁRIO GERAL (Biel 19/11): o galpão inteiro de uma vez, uma contagem por estrutura, conferido com o Bling produto por
produto (ver o bloco "inventário geral" abaixo). Rotas:
  GET  /inventario/geral                     o geral aberto (ou o último): contagens, produtos por situação, o que falta
  POST /inventario/geral                     admin/supervisor: começa (uma contagem por estrutura do mapa)
  POST /inventario/geral/proxima             pega para quem tocou a próxima contagem do geral
  POST /inventario/geral/<id>/item           {sku_key, acao: pegar|contar|nao_encontrei, qtd?, porta_palet?, posicao?}
  POST /inventario/geral/<id>/devolver       admin: {sku_key} o Não encontrei apareceu -> devolve o saldo no Bling
  POST /inventario/geral/<id>/buscar         admin/supervisor: procura de novo no Bling o que ninguém contou
  POST /inventario/geral/<id>/encerrar       admin: {cancelar?}

PEDIDO PARADO NA SEPARAÇÃO (Biel 19/11): GET /separacao/paradas, GET /separacao/paradas/<ml_id>/itens,
POST /separacao/paradas/<id>/responder {resposta: nao_achei|falta_produto|cancelado|termino_agora|outro, texto?, sku?}.
Ajustes: SEP_PARADA=1, SEP_PARADA_HORAS=2, SEP_PARADA_CORTE=15:30.

Ajustes (.env): INV_HORA=14:00, INV_DIAS=12345 (seg..sex), INV_AVISO_MIN=15, INV_AVISO_ADMIN=0, INV_BLING_ESCREVER=1, INV_LIGADO=1."""
from __future__ import annotations

import json
import math
import os
import threading
import time

from flask import jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

PG = bool(os.environ.get("DATABASE_URL"))
_ok_tab = False
_lk = threading.Lock()
_conferindo = set()


def _env(k, padrao):
    return (os.environ.get(k) or padrao).strip()


def INV_LIGADO():
    return _env("INV_LIGADO", "1") not in ("0", "false", "nao", "não", "off")


def _hora_inv():
    try:
        h, m = _env("INV_HORA", "14:00").split(":")
        return int(h), int(m)
    except Exception:
        return 14, 0


def _dias_uteis_cfg():
    return {int(c) for c in _env("INV_DIAS", "12345") if c.isdigit() and 1 <= int(c) <= 7}   # 1 = segunda ... 7 = domingo


# ─────────────────────────── tempo (Brasília) ───────────────────────────
def _br(ts=None):
    return time.gmtime((ts if ts is not None else time.time()) - 3 * 3600)


def _hoje():
    return time.strftime("%Y-%m-%d", _br())


def _mes():
    return time.strftime("%Y-%m", _br())


def _fim_do_dia_ts():
    t = _br()
    import calendar
    return calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday, 23, 59, 0, 0, 0, 0)) + 3 * 3600


def _eh_dia_util(t=None):
    t = t or _br()
    return (t.tm_wday + 1) in _dias_uteis_cfg()


def _dias_uteis_restantes(incluir_hoje=True):
    """Dias úteis do resto do mês (hoje conta se incluir_hoje)."""
    import calendar
    t = _br()
    ult = calendar.monthrange(t.tm_year, t.tm_mon)[1]
    n = 0
    for d in range(t.tm_mday if incluir_hoje else t.tm_mday + 1, ult + 1):
        wd = calendar.weekday(t.tm_year, t.tm_mon, d)            # 0 = segunda
        if (wd + 1) in _dias_uteis_cfg():
            n += 1
    return n


def _passou_da_hora():
    t = _br()
    h, m = _hora_inv()
    return (t.tm_hour, t.tm_min) >= (h, m)


# ─────────────────────────── quem ───────────────────────────
def _quem():
    try:
        return str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")[:80]
    except Exception:
        return ""


def _role():
    try:
        return str(getattr(current_user, "role", "") or "").lower()
    except Exception:
        return ""


def _gestor():
    return _role() in ("admin", "supervisor")


def _admin():
    return _role() == "admin"


def _key(s):
    return str(s or "").strip()[:64].lower()


# ─────────────────────────── tabelas ───────────────────────────
def _tabelas():
    global _ok_tab
    if _ok_tab:
        return
    pk = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with db_session() as c:
        c.execute(f"""CREATE TABLE IF NOT EXISTS tarefas (id {pk}, tipo TEXT NOT NULL DEFAULT 'livre', titulo TEXT NOT NULL,
            descricao TEXT, local TEXT, para TEXT NOT NULL DEFAULT '', criado_por TEXT, criado_em BIGINT, prazo_em BIGINT,
            status TEXT NOT NULL DEFAULT 'aberta', pego_por TEXT, pego_em BIGINT, feito_por TEXT, feito_em BIGINT, obs_fim TEXT, ref TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_tarefas_status ON tarefas (status, criado_em)")
        c.execute("CREATE TABLE IF NOT EXISTS tarefa_vista (tarefa_id BIGINT NOT NULL, usuario TEXT NOT NULL, em BIGINT, PRIMARY KEY (tarefa_id, usuario))")
        c.execute(f"""CREATE TABLE IF NOT EXISTS inv_contagem (id {pk}, tarefa_id BIGINT NOT NULL, rodada INTEGER NOT NULL DEFAULT 1,
            porta_palet INTEGER NOT NULL, posicao INTEGER NOT NULL, sku TEXT, sku_key TEXT, nome TEXT, qtd DOUBLE PRECISION,
            vazia INTEGER NOT NULL DEFAULT 0, achado INTEGER NOT NULL DEFAULT 0, por TEXT, em BIGINT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_inv_contagem_t ON inv_contagem (tarefa_id, rodada)")
        c.execute("""CREATE TABLE IF NOT EXISTS inv_pos (mes TEXT NOT NULL, porta_palet INTEGER NOT NULL, posicao INTEGER NOT NULL,
            tarefa_id BIGINT, em BIGINT, por TEXT, PRIMARY KEY (mes, porta_palet, posicao))""")
        c.execute(f"""CREATE TABLE IF NOT EXISTS inv_sku (id {pk}, tarefa_id BIGINT NOT NULL, rodada INTEGER NOT NULL, sku TEXT, sku_key TEXT,
            nome TEXT, contado DOUBLE PRECISION, bling DOUBLE PRECISION, separados DOUBLE PRECISION, status TEXT, msg TEXT, em BIGINT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_inv_sku_t ON inv_sku (tarefa_id, rodada)")
        c.execute(f"""CREATE TABLE IF NOT EXISTS inv_divergencia (id {pk}, tarefa_id BIGINT, mes TEXT, sku TEXT, sku_key TEXT, nome TEXT,
            locais TEXT, contado DOUBLE PRECISION, contado1 DOUBLE PRECISION, bling DOUBLE PRECISION, separados DOUBLE PRECISION,
            status TEXT NOT NULL DEFAULT 'aberta', criado_em BIGINT, contado_por TEXT, decidido_por TEXT, decidido_em BIGINT,
            valor DOUBLE PRECISION, bling_msg TEXT, visto INTEGER NOT NULL DEFAULT 0)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_inv_div_status ON inv_divergencia (status, criado_em)")
        c.execute("CREATE TABLE IF NOT EXISTS inv_dia (dia TEXT PRIMARY KEY, tarefa_id BIGINT, em BIGINT)")
        _tabelas_geral(c, pk)
        _tabela_parada(c, pk)
    _ok_tab = True


def _ins(c, sql, args):
    """INSERT ... RETURNING id (Postgres e SQLite novo)."""
    r = c.execute(sql + " RETURNING id", args).fetchone()
    return int(r["id"]) if r else None


def _tarefa(c, tid):
    r = c.execute("SELECT * FROM tarefas WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None


def _fmt(t, vistas=None):
    ref = {}
    try:
        ref = json.loads(t.get("ref") or "{}") or {}
    except Exception:
        pass
    out = {k: t.get(k) for k in ("id", "tipo", "titulo", "descricao", "local", "para", "criado_por", "criado_em", "prazo_em",
                                  "status", "pego_por", "pego_em", "feito_por", "feito_em", "obs_fim")}
    if t.get("tipo") == "inventario":
        out["inventario"] = {"posicoes": len(ref.get("posicoes") or []), "estruturas": ref.get("estruturas") or [],
                             "rodada": ref.get("rodada", 1), "recontar": len(ref.get("recontar") or []), "dia": ref.get("dia"),
                             "so_skus": bool(ref.get("so_skus")), "geral": ref.get("geral"),
                             "geral_recontar": bool(ref.get("geral_recontar"))}
    if vistas is not None:
        out["vista"] = t["id"] in vistas
    return out


# ─────────────────────────── mapa ───────────────────────────
def _layout():
    try:
        import mapa_layout as ML
        lay, _ = ML.layout()
        return ML, lay
    except Exception:
        return None, None


def _estruturas_em_ordem():
    """[(estrutura, [posições...])] na ordem da rota."""
    ML, lay = _layout()
    if not lay:
        return [], None
    out = []
    for e in lay.get("estruturas") or []:
        ps = list(ML.posicoes(e))
        if ps:
            out.append((e, ps))
    out.sort(key=lambda x: ML.rank(x[0]["id"], min(x[1])))
    return out, ML


def _skus_no_mapa(c):
    """{(pp, p): [{sku, nome, foto}]} e {sku_key: [(pp, p)]}."""
    por_pos, por_sku = {}, {}
    for r in c.execute("SELECT sku, sku_key, nome, foto, porta_palet, posicao FROM mapa_localizacao").fetchall():
        k = (int(r["porta_palet"]), int(r["posicao"]))
        por_pos.setdefault(k, []).append({"sku": r["sku"], "nome": r["nome"] or r["sku"], "foto": r["foto"]})
        por_sku.setdefault(r["sku_key"] or _key(r["sku"]), []).append(k)
    return por_pos, por_sku


def _plano(c, mes=None):
    mes = mes or _mes()
    est, ML = _estruturas_em_ordem()
    contadas = {(int(r["porta_palet"]), int(r["posicao"])) for r in
                c.execute("SELECT porta_palet, posicao FROM inv_pos WHERE mes=?", (mes,)).fetchall()}
    hoje = c.execute("SELECT tarefa_id FROM inv_dia WHERE dia=?", (_hoje(),)).fetchone()
    t_hoje = _tarefa(c, int(hoje["tarefa_id"])) if hoje and hoje["tarefa_id"] else None
    pos_hoje = set()
    if t_hoje and t_hoje["status"] not in ("cancelada", "expirada"):
        try:
            pos_hoje = {tuple(x) for x in (json.loads(t_hoje["ref"] or "{}").get("posicoes") or [])}
        except Exception:
            pass
    total = sum(len(ps) for _, ps in est)
    feitas = sum(1 for e, ps in est for p in ps if (int(e["id"]), int(p)) in contadas)
    falta = total - feitas
    dias = _dias_uteis_restantes(incluir_hoje=_eh_dia_util())
    cota = math.ceil(falta / dias) if dias and falta else falta
    estrs = []
    for e, ps in est:
        n_c = sum(1 for p in ps if (int(e["id"]), int(p)) in contadas)
        n_h = sum(1 for p in ps if (int(e["id"]), int(p)) in pos_hoje)
        st = "contado" if n_c == len(ps) else ("hoje" if n_h and t_hoje and t_hoje["status"] != "feita" else ("parcial" if n_c else "falta"))
        estrs.append({"id": e["id"], "curto": e.get("curto") or e.get("nome"), "tipo": e.get("tipo"), "posicoes": len(ps),
                      "contadas": n_c, "status": st})
    # ritmo: o esperado até hoje se cada dia útil contasse igual
    import calendar
    t = _br()
    ult = calendar.monthrange(t.tm_year, t.tm_mon)[1]
    uteis_mes = sum(1 for d in range(1, ult + 1) if (calendar.weekday(t.tm_year, t.tm_mon, d) + 1) in _dias_uteis_cfg())
    uteis_passados = sum(1 for d in range(1, t.tm_mday) if (calendar.weekday(t.tm_year, t.tm_mon, d) + 1) in _dias_uteis_cfg())
    # o inventário pode começar no meio do mês: o ritmo conta a partir do primeiro dia com tarefa neste mês
    r0 = c.execute("SELECT MIN(dia) AS d FROM inv_dia WHERE dia LIKE ?", (mes + "%",)).fetchone()
    d0 = int(str(r0["d"])[8:10]) if r0 and r0["d"] else t.tm_mday
    uteis_mes = sum(1 for d in range(d0, ult + 1) if (calendar.weekday(t.tm_year, t.tm_mon, d) + 1) in _dias_uteis_cfg())
    uteis_passados = sum(1 for d in range(d0, t.tm_mday) if (calendar.weekday(t.tm_year, t.tm_mon, d) + 1) in _dias_uteis_cfg())
    esperado = round(total * uteis_passados / uteis_mes) if uteis_mes else 0
    return {"mes": mes, "total": total, "contadas": feitas, "falta": falta, "dias_uteis_restantes": dias, "cota_hoje": cota,
            "esperado_ate_ontem": esperado, "ritmo": "no ritmo" if feitas >= esperado else "atrasado",
            "estruturas": estrs, "tarefa_hoje": _fmt(t_hoje) if t_hoje else None,
            "hora": "%02d:%02d" % _hora_inv(), "dia_util": _eh_dia_util(), "ligado": INV_LIGADO()}


def _rotulo(ML, pp, p):
    try:
        return ML.rotulo(pp, p)
    except Exception:
        return f"{pp} · P{p}"


def _gerar_hoje(forcar=False):
    """Monta (uma vez por dia) a tarefa de inventário de hoje. -> tarefa (dict) ou None."""
    _tabelas()
    if not forcar:
        with db_session() as c:
            r = c.execute("SELECT tarefa_id FROM inv_dia WHERE dia=?", (_hoje(),)).fetchone()
            if r:
                return _tarefa(c, int(r["tarefa_id"])) if r["tarefa_id"] else None
    if not INV_LIGADO() and not forcar:
        return None
    if not forcar and (not _eh_dia_util() or not _passou_da_hora()):
        return None
    with db_session() as c:
        if _geral_aberto(c):           # inventário geral aberto: rotativo pausado
            return None
    with _lk:
        with db_session() as c:
            r = c.execute("SELECT tarefa_id FROM inv_dia WHERE dia=?", (_hoje(),)).fetchone()
            if r:
                return _tarefa(c, int(r["tarefa_id"])) if r["tarefa_id"] else None
            # tarefa de inventário de outro dia que ninguém terminou: expira (o que não foi contado volta para a fila do mês)
            c.execute("UPDATE tarefas SET status='expirada' WHERE tipo='inventario' AND status IN ('aberta','andamento','recontar') "
                      "AND COALESCE(ref,'') NOT LIKE ? AND prazo_em < ?", ('%"so_skus"%', int(time.time())))
            plano = _plano(c)
            est, ML = _estruturas_em_ordem()
            contadas = {(int(x["porta_palet"]), int(x["posicao"])) for x in
                        c.execute("SELECT porta_palet, posicao FROM inv_pos WHERE mes=?", (_mes(),)).fetchall()}
            cota = plano["cota_hoje"]
            if not cota:
                c.execute("INSERT INTO inv_dia (dia, tarefa_id, em) VALUES (?,?,?)", (_hoje(), 0, int(time.time())))
                return None
            sel, nomes, ids = [], [], []
            for e, ps in est:
                livres = [(int(e["id"]), int(p)) for p in ps if (int(e["id"]), int(p)) not in contadas]
                if not livres:
                    continue
                sel += livres
                nomes.append(e.get("curto") or e.get("nome"))
                ids.append(e["id"])
                if len(sel) >= cota:
                    break
            # produto que está também em outro lugar entra inteiro (a soma por produto tem que ser completa)
            por_pos, por_sku = _skus_no_mapa(c)
            extra = []
            for k in list(sel):
                for it in por_pos.get(k, []):
                    for k2 in por_sku.get(_key(it["sku"]), []):
                        if k2 not in sel and k2 not in extra:
                            extra.append(k2)
            todas = sorted(sel + extra, key=lambda k: ML.rank(*k) if ML else 0)
            titulo = "Inventário: contar " + (" e ".join([", ".join(nomes[:-1]), nomes[-1]]) if len(nomes) > 1 else nomes[0])
            if extra:
                titulo += f" (+{len(extra)} posição{'ões' if len(extra) > 1 else ''} de outros lugares)"
            ref = {"mes": _mes(), "dia": _hoje(), "posicoes": [list(k) for k in todas], "estruturas": ids, "rodada": 1,
                   "forcado": bool(forcar and not _passou_da_hora())}
            n_prod = len({_key(it["sku"]) for k in todas for it in por_pos.get(k, [])})
            desc = f"{len(todas)} posições, {n_prod} produtos. Conte tudo o que tem em cada posição; o sistema confere com o Bling."
            tid = _ins(c, "INSERT INTO tarefas (tipo, titulo, descricao, para, criado_por, criado_em, prazo_em, status, ref) "
                          "VALUES ('inventario', ?, ?, '', 'WMS', ?, ?, 'aberta', ?)",
                       (titulo[:200], desc, int(time.time()), _fim_do_dia_ts(), json.dumps(ref)))
            c.execute("INSERT INTO inv_dia (dia, tarefa_id, em) VALUES (?,?,?)", (_hoje(), tid, int(time.time())))
            return _tarefa(c, tid)


# ─────────────────────────── Bling ───────────────────────────
def _bling():
    import naoachado_routes as NA
    from services.bling_service import BlingService
    return NA, BlingService()


def _saldo_bling(sku):
    """-> (saldo, produto_id, msg_erro)."""
    try:
        NA, bs = _bling()
        try:
            pid = NA._produto_id(bs, sku)
        except LookupError as e:
            return None, None, str(e)
        return NA._saldo_no_deposito(bs, pid, NA._deposito()), pid, None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"[:300]


def _separados(c, sku):
    """Unidades do produto fora da prateleira esperando sair: bipadas na Separação (pedido pendente/parcial), separadas
    esperando a Expedição, e separadas do Full em remessa que ainda não foi enviada."""
    n = 0.0
    try:
        r = c.execute("""SELECT COALESCE(SUM(CASE WHEN p.status IN ('PENDENTE','PARCIAL') THEN pp.quantidade_bipada
                                                 WHEN p.status = 'SEPARADO' THEN COALESCE(pp.quantidade_separada, 0) + pp.quantidade_bipada
                                                 ELSE 0 END), 0) AS n
                           FROM pedido_produtos pp JOIN produtos pr ON pr.id = pp.produto_id JOIN pedidos p ON p.id = pp.pedido_id
                          WHERE UPPER(pr.sku) = UPPER(?) AND p.status IN ('PENDENTE','PARCIAL','SEPARADO')""", (sku,)).fetchone()
        n += float(r["n"] or 0)
    except Exception:
        pass
    try:
        r = c.execute("""SELECT COALESCE(SUM(i.separado), 0) AS n FROM full_itens i JOIN full_remessas r ON r.id = i.remessa_id
                          WHERE UPPER(i.sku) = UPPER(?) AND r.status NOT IN ('enviada','cancelada')""", (sku,)).fetchone()
        n += float(r["n"] or 0)
    except Exception:
        pass
    return n


def _conferir(tid):
    """Segundo plano: soma a rodada por produto e compara com o Bling."""
    try:
        with db_session() as c:
            t = _tarefa(c, tid)
            ref = json.loads(t["ref"] or "{}")
            rod = int(ref.get("rodada", 1))
            linhas = [dict(r) for r in c.execute("SELECT * FROM inv_contagem WHERE tarefa_id=? AND rodada=?", (tid, rod)).fetchall()]
        so = set(ref.get("recontar") or []) if rod > 1 else None
        tot, nomes, locais, achados = {}, {}, {}, []
        for l in linhas:
            if l["vazia"] or not l["sku_key"]:
                continue
            if so is not None and l["sku_key"] not in so:
                continue
            tot[l["sku_key"]] = tot.get(l["sku_key"], 0) + float(l["qtd"] or 0)
            nomes[l["sku_key"]] = (l["sku"], l["nome"])
            locais.setdefault(l["sku_key"], []).append([int(l["porta_palet"]), int(l["posicao"]), float(l["qtd"] or 0)])
            if l["achado"]:
                achados.append(l)
        # produto que o mapa diz estar numa posição contada e não foi digitado = 0 (posição marcada vazia ou esquecido)
        with db_session() as c:
            por_pos, _ = _skus_no_mapa(c)
        for k in {(int(l["porta_palet"]), int(l["posicao"])) for l in linhas}:
            for it in por_pos.get(k, []):
                kk = _key(it["sku"])
                if (so is None or kk in so) and kk not in tot:
                    tot[kk] = 0.0
                    nomes[kk] = (it["sku"], it["nome"])
                    locais.setdefault(kk, []).append([k[0], k[1], 0.0])
        recontar, div = [], []
        anterior = {}
        if rod > 1:
            with db_session() as c:
                for r in c.execute("SELECT sku_key, contado FROM inv_sku WHERE tarefa_id=? AND rodada=?", (tid, rod - 1)).fetchall():
                    anterior[r["sku_key"]] = float(r["contado"] or 0)
        for kk, n in tot.items():
            sku, nome = nomes[kk]
            saldo, _, erro = _saldo_bling(sku)
            time.sleep(0.25)
            with db_session() as c:
                sep = _separados(c, sku)
            if erro is None and (abs(n - saldo) < 1e-6 or (sep > 0 and abs(n + sep - saldo) < 1e-6)):
                st = "ok"
            elif rod == 1 and erro is None:
                st = "recontar"
                recontar.append(kk)
            else:
                st = "divergente"
                div.append((kk, sku, nome, n, saldo, sep, erro))
            with db_session() as c:
                c.execute("INSERT INTO inv_sku (tarefa_id, rodada, sku, sku_key, nome, contado, bling, separados, status, msg, em) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (tid, rod, sku, kk, nome, n, saldo, sep, st, erro, int(time.time())))
        with db_session() as c:
            t = _tarefa(c, tid)
            ref = json.loads(t["ref"] or "{}")
            # achou produto numa posição que o mapa não tinha: cadastra no mapa (ele está lá)
            for l in achados:
                try:
                    if not c.execute("SELECT 1 FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?",
                                     (l["sku_key"], l["porta_palet"], l["posicao"])).fetchone():
                        c.execute("INSERT INTO mapa_localizacao (sku_key, sku, nome, porta_palet, posicao, atualizado_em) VALUES (?,?,?,?,?,?)",
                                  (l["sku_key"], l["sku"], l["nome"], l["porta_palet"], l["posicao"], int(time.time())))
                except Exception:
                    pass
            for kk, sku, nome, n, saldo, sep, erro in div:
                _ins(c, "INSERT INTO inv_divergencia (tarefa_id, mes, sku, sku_key, nome, locais, contado, contado1, bling, separados, "
                        "status, criado_em, contado_por, bling_msg) VALUES (?,?,?,?,?,?,?,?,?,?,'aberta',?,?,?)",
                     (tid, ref.get("mes") or _mes(), sku, kk, nome, json.dumps(locais.get(kk) or []), n, anterior.get(kk), saldo, sep,
                      int(time.time()), t.get("pego_por"), erro))
            if recontar:
                ref["rodada"] = rod + 1
                ref["recontar"] = recontar
                c.execute("UPDATE tarefas SET status='recontar', ref=? WHERE id=?", (json.dumps(ref), tid))
            else:
                for pp, p in ref.get("posicoes") or []:
                    c.execute("DELETE FROM inv_pos WHERE mes=? AND porta_palet=? AND posicao=?", (ref.get("mes") or _mes(), pp, p))
                    c.execute("INSERT INTO inv_pos (mes, porta_palet, posicao, tarefa_id, em, por) VALUES (?,?,?,?,?,?)",
                              (ref.get("mes") or _mes(), pp, p, tid, int(time.time()), t.get("pego_por")))
                c.execute("UPDATE tarefas SET status='feita', feito_por=COALESCE(feito_por, pego_por), feito_em=? WHERE id=?",
                          (int(time.time()), tid))
    except Exception as e:
        try:
            with db_session() as c:
                c.execute("UPDATE tarefas SET status='andamento', obs_fim=? WHERE id=?", (f"Conferência falhou: {e}"[:300], tid))
        except Exception:
            pass
    finally:
        _conferindo.discard(tid)


# ─────────────────────────── inventário geral (Biel 19/11) ───────────────────────────
# "Vamos fazer um inventário cheio do galpão inteiro ... ir batendo como está o fluxo hoje do inventário."
# Começar (admin/supervisor): uma contagem por estrutura do mapa, todas de uma vez; cada um pega uma e conta às cegas.
# Bate por PRODUTO, não por contagem: quando todas as posições de um produto no mapa já foram contadas (por quem for),
# o WMS soma e confere com o Bling (contado + separado esperando sair). Não bateu: recontagem só desse produto (1 vez);
# de novo não bateu: divergência para o admin (a mesma de hoje). Contagem que chega depois (achado em outra posição)
# confere de novo. Rotativo das 14h pausado enquanto o geral está aberto; posição contada vale como contada no mês.
# Fim das estruturas: o WMS lê no Bling tudo o que tem saldo no depósito e ninguém contou -> pop-up piscando (Biel:
# "não existe esse de não contar"): Contar (às cegas; não bateu = conta de novo 1 vez; de novo = admin) ou Não encontrei
# (zera o saldo do depósito no Bling; o admin devolve se acharem). Sem nada pendente, o geral encerra sozinho.
GERAL_ABERTO = ("contando", "buscando", "procurando")
GERAL_TRAVA_S = 600                     # quem tocou em Contar no pop-up fica com o produto 10 min
_lk_geral = threading.Lock()            # conferência (segundo plano, com Bling) e começar/encerrar
_lk_pega = threading.Lock()             # pegar contagem / produto do pop-up (rápido: não espera a conferência)


def _tabelas_geral(c, pk):
    c.execute(f"""CREATE TABLE IF NOT EXISTS inv_geral (id {pk}, status TEXT NOT NULL DEFAULT 'contando', mes TEXT,
        criado_por TEXT, criado_em BIGINT, fase2_em BIGINT, encerrado_por TEXT, encerrado_em BIGINT, msg TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS inv_geral_tarefa (geral_id BIGINT NOT NULL, tarefa_id BIGINT NOT NULL, tipo TEXT NOT NULL,
        sku_key TEXT, PRIMARY KEY (geral_id, tarefa_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_inv_geral_tarefa_t ON inv_geral_tarefa (tarefa_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS inv_geral_sku (geral_id BIGINT NOT NULL, sku_key TEXT NOT NULL, sku TEXT, nome TEXT,
        status TEXT, contado DOUBLE PRECISION, contado1 DOUBLE PRECISION, bling DOUBLE PRECISION, separados DOUBLE PRECISION,
        recont_tid BIGINT, div_id BIGINT, msg TEXT, por TEXT, trava_por TEXT, trava_em BIGINT, em BIGINT,
        PRIMARY KEY (geral_id, sku_key))""")
    c.execute(f"""CREATE TABLE IF NOT EXISTS inv_geral_extra (id {pk}, geral_id BIGINT NOT NULL, sku_key TEXT NOT NULL, sku TEXT,
        nome TEXT, qtd DOUBLE PRECISION, porta_palet INTEGER, posicao INTEGER, por TEXT, em BIGINT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_inv_geral_extra_g ON inv_geral_extra (geral_id, sku_key)")


def _geral_aberto(c):
    r = c.execute("SELECT * FROM inv_geral WHERE status IN ('contando','buscando','procurando') ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def _geral_de_tarefa(c, tid):
    r = c.execute("SELECT geral_id, tipo, sku_key FROM inv_geral_tarefa WHERE tarefa_id=?", (tid,)).fetchone()
    return dict(r) if r else None


def _gsku(c, gid, kk):
    r = c.execute("SELECT * FROM inv_geral_sku WHERE geral_id=? AND sku_key=?", (gid, kk)).fetchone()
    return dict(r) if r else None


def _gsku_gravar(c, gid, kk, **campos):
    campos["em"] = int(time.time())
    if _gsku(c, gid, kk):
        sets = ", ".join(f"{k}=?" for k in campos)
        c.execute(f"UPDATE inv_geral_sku SET {sets} WHERE geral_id=? AND sku_key=?", (*campos.values(), gid, kk))
    else:
        campos.update({"geral_id": gid, "sku_key": kk})
        c.execute(f"INSERT INTO inv_geral_sku ({', '.join(campos)}) VALUES ({', '.join('?' * len(campos))})", tuple(campos.values()))


def _geral_tarefas(c, gid):
    """{tarefa_id: {tipo, sku_key, status, pego_por}}"""
    out = {}
    for r in c.execute("SELECT g.tarefa_id, g.tipo, g.sku_key, t.status, t.pego_por FROM inv_geral_tarefa g "
                       "JOIN tarefas t ON t.id = g.tarefa_id WHERE g.geral_id=?", (gid,)).fetchall():
        out[int(r["tarefa_id"])] = {"tipo": r["tipo"], "sku_key": r["sku_key"], "status": r["status"], "pego_por": r["pego_por"]}
    return out


def _contagem_geral(c, gid):
    """Contagens das contagens FEITAS do geral.
    -> (por_sku {kk: {(pp, p): qtd}}, nomes {kk: (sku, nome)}, posições contadas, extra {kk: qtd do pop-up}).
    Vale a última contagem de cada produto em cada posição (recontagem substitui). Posição vazia = 0 para os produtos que o
    mapa diz estar lá (contagem de estrutura) ou para o produto da recontagem."""
    tars = {t: v for t, v in _geral_tarefas(c, gid).items() if v["status"] == "feita"}
    por_pos, _ = _skus_no_mapa(c)
    ult, nomes, pos_cont = {}, {}, set()
    ids = list(tars)
    for i in range(0, len(ids), 200):
        grupo = ids[i:i + 200]
        for r in c.execute(f"SELECT id, tarefa_id, porta_palet, posicao, sku, sku_key, nome, qtd, vazia FROM inv_contagem "
                           f"WHERE tarefa_id IN ({','.join('?' * len(grupo))}) AND rodada=1", tuple(grupo)).fetchall():
            tv = tars[int(r["tarefa_id"])]
            k = (int(r["porta_palet"]), int(r["posicao"]))
            pos_cont.add(k)
            if r["vazia"]:
                alvo = [tv["sku_key"]] if tv["tipo"] == "recontar" else [_key(it["sku"]) for it in por_pos.get(k, [])]
                for kk in alvo:
                    if kk and int(r["id"]) > ult.get((kk, k), (0, 0))[0]:
                        ult[(kk, k)] = (int(r["id"]), 0.0)
                continue
            kk = r["sku_key"] or _key(r["sku"])
            if not kk:
                continue
            if int(r["id"]) > ult.get((kk, k), (0, 0))[0]:
                ult[(kk, k)] = (int(r["id"]), float(r["qtd"] or 0))
            if r["sku"]:
                nomes[kk] = (r["sku"], r["nome"] or (nomes.get(kk) or (None, None))[1])
    por_sku = {}
    for (kk, k), (_, q) in ult.items():
        por_sku.setdefault(kk, {})[k] = q
    for k, its in por_pos.items():                      # nome de quem só apareceu como posição vazia
        for it in its:
            kk = _key(it["sku"])
            if kk in por_sku and (kk not in nomes or not nomes[kk][1]):
                nomes[kk] = (it["sku"], it["nome"])
    extra = {}
    for r in c.execute("SELECT sku_key, sku, nome, qtd FROM inv_geral_extra WHERE geral_id=? ORDER BY id", (gid,)).fetchall():
        extra[r["sku_key"]] = float(r["qtd"] or 0)       # a última vale (contar de novo substitui)
        nomes.setdefault(r["sku_key"], (r["sku"], r["nome"]))
    return por_sku, nomes, pos_cont, extra


def _ordem(ML, ks):
    try:
        return sorted(ks, key=lambda k: ML.rank(*k))
    except Exception:
        return sorted(ks)


def _criar_recontagem(c, gid, kk, sku, nome, mapa, contados):
    ks = {tuple(int(x) for x in k) for k in (mapa or [])} | {k for k in (contados or {}) if isinstance(k, tuple)}
    if not ks:
        return None
    _, ML = _estruturas_em_ordem()
    pos = _ordem(ML, ks)
    ref = {"geral": gid, "mes": _mes(), "dia": _hoje(), "posicoes": [list(k) for k in pos], "estruturas": [], "rodada": 1,
           "so_skus": [kk], "geral_recontar": kk}
    tid = _ins(c, "INSERT INTO tarefas (tipo, titulo, descricao, para, criado_por, criado_em, prazo_em, status, ref) "
                  "VALUES ('inventario', ?, ?, '', 'WMS', ?, ?, 'aberta', ?)",
               (f"Inventário geral: recontar {nome or sku}"[:200],
                f"{len(pos)} posição(ões). Não bateu com o Bling: conte de novo esse produto, com calma.",
                int(time.time()), int(time.time()) + 30 * 86400, json.dumps(ref)))
    c.execute("INSERT INTO inv_geral_tarefa (geral_id, tarefa_id, tipo, sku_key) VALUES (?,?,?,?)", (gid, tid, "recontar", kk))
    return tid


def _avaliar(gid, kks, origem, forcar=False, quem=None):
    """Confere com o Bling os produtos kks do geral. origem: estrutura | recontagem | extra | fase2.
    forcar: confere mesmo se alguma posição do mapa do produto não foi contada (fim das estruturas)."""
    with _lk_geral:
        with db_session() as c:
            g = c.execute("SELECT status FROM inv_geral WHERE id=?", (gid,)).fetchone()
            if not g or g["status"] not in GERAL_ABERTO:
                return
            cont, nomes, pos_cont, extra = _contagem_geral(c, gid)
            _, por_sku_map = _skus_no_mapa(c)
        for kk in kks:
            with db_session() as c:
                atual = _gsku(c, gid, kk) or {}
            st0 = atual.get("status")
            if st0 in ("nao_encontrado",) or (kk not in cont and kk not in extra):
                continue
            total = sum(cont.get(kk, {}).values()) + extra.get(kk, 0.0)
            sku, nome = nomes.get(kk) or (atual.get("sku") or kk, atual.get("nome"))
            nome = nome or atual.get("nome")
            mapa = por_sku_map.get(kk) or []
            if not forcar and origem == "estrutura" and not all(tuple(k) in pos_cont for k in mapa):
                if st0 in (None, "esperando"):
                    with db_session() as c:
                        _gsku_gravar(c, gid, kk, sku=sku, nome=nome, status="esperando", contado=total)
                continue
            saldo, _, erro = _saldo_bling(sku)
            time.sleep(0.25)
            with db_session() as c:
                sep = _separados(c, sku)
                bate = erro is None and (abs(total - saldo) < 1e-6 or (sep > 0 and abs(total + sep - saldo) < 1e-6))
                locais = [[k[0], k[1], q] for k, q in sorted(cont.get(kk, {}).items())]
                if kk in extra:
                    locais.append([0, 0, extra[kk]])
                if bate:
                    if st0 == "recontar" and atual.get("recont_tid"):
                        c.execute("UPDATE tarefas SET status='cancelada', obs_fim=? WHERE id=? AND status IN ('aberta','andamento')",
                                  ("Não precisa mais: o produto bateu com o Bling.", atual["recont_tid"]))
                    if atual.get("div_id"):
                        c.execute("UPDATE inv_divergencia SET status='resolvida', contado=?, bling=?, separados=?, decidido_por='WMS', "
                                  "decidido_em=?, bling_msg=? WHERE id=? AND status='aberta'",
                                  (total, saldo, sep, int(time.time()), "Inventário geral: com a contagem de outra posição passou a bater.",
                                   atual["div_id"]))
                    _gsku_gravar(c, gid, kk, sku=sku, nome=nome, status="ok", contado=total, bling=saldo, separados=sep, msg=None)
                    continue
                segunda = (origem == "recontagem" and st0 == "recontar") or (origem == "extra" and st0 == "contar_de_novo")
                if st0 == "divergente":
                    c.execute("UPDATE inv_divergencia SET contado=?, bling=?, separados=?, locais=? WHERE id=? AND status='aberta'",
                              (total, saldo, sep, json.dumps(locais), atual.get("div_id")))
                    _gsku_gravar(c, gid, kk, contado=total, bling=saldo, separados=sep)
                elif erro or segunda:
                    did = _ins(c, "INSERT INTO inv_divergencia (tarefa_id, mes, sku, sku_key, nome, locais, contado, contado1, bling, "
                                  "separados, status, criado_em, contado_por, bling_msg) VALUES (?,?,?,?,?,?,?,?,?,?,'aberta',?,?,?)",
                               (atual.get("recont_tid") or 0, _mes(), sku, kk, nome, json.dumps(locais), total, atual.get("contado1"),
                                saldo, sep, int(time.time()), quem, erro))
                    _gsku_gravar(c, gid, kk, sku=sku, nome=nome, status="divergente", div_id=did, contado=total, bling=saldo,
                                 separados=sep, msg=erro, trava_por=None, trava_em=None)
                elif st0 == "recontar":
                    _gsku_gravar(c, gid, kk, contado=total)           # recontagem já pedida: espera ela
                elif origem in ("extra",) or not (mapa or cont.get(kk)):
                    _gsku_gravar(c, gid, kk, sku=sku, nome=nome, status="contar_de_novo", contado1=total, contado=total,
                                 bling=saldo, separados=sep, trava_por=None, trava_em=None, msg=None)
                else:
                    tid = _criar_recontagem(c, gid, kk, sku, nome, mapa, cont.get(kk))
                    _gsku_gravar(c, gid, kk, sku=sku, nome=nome, status="recontar" if tid else "contar_de_novo", contado1=total,
                                 contado=total, recont_tid=tid, bling=saldo, separados=sep)
    _checar_fim(gid)


def _checar_fim(gid):
    iniciar = False
    with db_session() as c:
        g = c.execute("SELECT * FROM inv_geral WHERE id=?", (gid,)).fetchone()
        if not g:
            return
        tars = _geral_tarefas(c, gid)
        abertas = lambda tp: [t for t, v in tars.items() if v["tipo"] == tp and v["status"] not in ("feita", "cancelada", "expirada")]
        if g["status"] == "contando" and not abertas("estrutura"):
            c.execute("UPDATE inv_geral SET status='buscando', fase2_em=?, msg=? WHERE id=? AND status='contando'",
                      (int(time.time()), "Todas as estruturas contadas. Procurando no Bling o que ninguém contou…", gid))
            iniciar = True
        elif g["status"] == "procurando" and not abertas("recontar"):
            n = c.execute("SELECT COUNT(*) AS n FROM inv_geral_sku WHERE geral_id=? AND status IN ('nao_contado','contar_de_novo','esperando')",
                          (gid,)).fetchone()["n"]
            if not n:
                c.execute("UPDATE inv_geral SET status='encerrado', encerrado_por='WMS', encerrado_em=?, msg=? WHERE id=? AND status='procurando'",
                          (int(time.time()), "Tudo contado e conferido. O que não bateu está nas divergências.", gid))
    if iniciar:
        threading.Thread(target=_fase2, args=(gid,), daemon=True).start()


def _bling_com_saldo():
    """{sku_key: (sku, nome, saldo)} de todo produto simples ativo do Bling com saldo físico > 0 no depósito."""
    NA, bs = _bling()
    dep = str(NA._deposito())
    prods = {}
    for pag in range(1, 201):
        r = NA._bling_req(bs._get, "/produtos", params={"pagina": pag, "limite": 100, "criterio": 2})
        if r.status_code != 200:
            raise RuntimeError(f"Bling produtos HTTP {r.status_code}")
        d = (r.json() or {}).get("data") or []
        for p in d:
            if str(p.get("formato") or "S").upper() != "S" or str(p.get("tipo") or "P").upper() == "S":
                continue                                   # kit/composição, pai com variações, serviço
            cod = str(p.get("codigo") or "").strip()
            if cod and p.get("id"):
                prods[int(p["id"])] = (cod, str(p.get("nome") or "")[:200])
        if len(d) < 100:
            break
        time.sleep(0.35)
    out, ids = {}, list(prods)
    for i in range(0, len(ids), 50):
        grupo = ids[i:i + 50]
        r = NA._bling_req(bs._get, "/estoques/saldos", params=[("idsProdutos[]", str(x)) for x in grupo])
        if r.status_code != 200:
            raise RuntimeError(f"Bling saldos HTTP {r.status_code}")
        for row in (r.json() or {}).get("data") or []:
            pid = (row.get("produto") or {}).get("id")
            if pid is None or int(pid) not in prods:
                continue
            saldo = 0.0
            for dd in row.get("depositos") or []:
                if str(dd.get("id")) == dep:
                    saldo = float(dd.get("saldoFisico") or 0)
            if saldo > 0:
                cod, nome = prods[int(pid)]
                out[_key(cod)] = (cod, nome, saldo)
        time.sleep(0.35)
    return out


def _fase2(gid):
    try:
        with db_session() as c:
            esp = [r["sku_key"] for r in c.execute("SELECT sku_key FROM inv_geral_sku WHERE geral_id=? AND status='esperando'", (gid,)).fetchall()]
        if esp:                     # alguma posição do produto ficou fora das estruturas: confere com o que foi contado
            _avaliar(gid, esp, "fase2", forcar=True)
        com_saldo = _bling_com_saldo()
        with _lk_geral:
            with db_session() as c:
                cont, _, _, extra = _contagem_geral(c, gid)
                ja = {r["sku_key"] for r in c.execute("SELECT sku_key FROM inv_geral_sku WHERE geral_id=?", (gid,)).fetchall()}
                n = 0
                for kk, (sku, nome, saldo) in com_saldo.items():
                    if kk in cont or kk in extra or kk in ja:
                        continue
                    _gsku_gravar(c, gid, kk, sku=sku, nome=nome, status="nao_contado", bling=saldo)
                    n += 1
                c.execute("UPDATE inv_geral SET status='procurando', msg=? WHERE id=? AND status='buscando'",
                          (f"{n} produto(s) com saldo no Bling que ninguém contou." if n else "Nada ficou sem contar.", gid))
    except Exception as e:
        try:
            with db_session() as c:
                c.execute("UPDATE inv_geral SET msg=? WHERE id=?", (f"Não consegui ler o Bling: {type(e).__name__}: {e}"[:300], gid))
        except Exception:
            pass
        return                      # fica 'buscando': admin/supervisor toca em "Procurar de novo"
    _checar_fim(gid)


def _enviar_geral(tid):
    """Segundo plano: contagem de estrutura (ou recontagem) do geral enviada -> feita, e confere os produtos dela."""
    gid, kks, tipo, quem = None, set(), "estrutura", None
    try:
        with db_session() as c:
            t = _tarefa(c, tid)
            ref = json.loads(t["ref"] or "{}")
            g = _geral_de_tarefa(c, tid)
            gid, tipo, quem = int(g["geral_id"]), g["tipo"], t.get("pego_por")
            linhas = [dict(r) for r in c.execute("SELECT * FROM inv_contagem WHERE tarefa_id=? AND rodada=1", (tid,)).fetchall()]
            for l in linhas:               # achou produto numa posição que o mapa não tinha: cadastra no mapa (ele está lá)
                if l["achado"] and l["sku_key"]:
                    try:
                        if not c.execute("SELECT 1 FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?",
                                         (l["sku_key"], l["porta_palet"], l["posicao"])).fetchone():
                            c.execute("INSERT INTO mapa_localizacao (sku_key, sku, nome, porta_palet, posicao, atualizado_em) VALUES (?,?,?,?,?,?)",
                                      (l["sku_key"], l["sku"], l["nome"], l["porta_palet"], l["posicao"], int(time.time())))
                    except Exception:
                        pass
            if tipo == "estrutura":        # vale como contado no mês (o rotativo não conta de novo)
                for pp, p in ref.get("posicoes") or []:
                    c.execute("DELETE FROM inv_pos WHERE mes=? AND porta_palet=? AND posicao=?", (ref.get("mes") or _mes(), pp, p))
                    c.execute("INSERT INTO inv_pos (mes, porta_palet, posicao, tarefa_id, em, por) VALUES (?,?,?,?,?,?)",
                              (ref.get("mes") or _mes(), pp, p, tid, int(time.time()), quem))
            c.execute("UPDATE tarefas SET status='feita', feito_por=COALESCE(feito_por, pego_por), feito_em=? WHERE id=?", (int(time.time()), tid))
            por_pos, _ = _skus_no_mapa(c)
            if tipo == "recontar":
                kks = {g["sku_key"]}
            else:
                kks = {l["sku_key"] for l in linhas if l["sku_key"]}
                for k in {(int(l["porta_palet"]), int(l["posicao"])) for l in linhas}:
                    kks |= {_key(it["sku"]) for it in por_pos.get(k, [])}
    except Exception as e:
        try:
            with db_session() as c:
                c.execute("UPDATE tarefas SET status='andamento', obs_fim=? WHERE id=?", (f"Envio falhou: {e}"[:300], tid))
        except Exception:
            pass
        _conferindo.discard(tid)
        return
    _conferindo.discard(tid)
    try:
        _avaliar(gid, sorted(kks), "recontagem" if tipo == "recontar" else "estrutura", quem=quem)
    except Exception as e:
        try:
            with db_session() as c:
                c.execute("UPDATE inv_geral SET msg=? WHERE id=?", (f"Conferência falhou: {type(e).__name__}: {e}"[:300], gid))
        except Exception:
            pass


def _escrever_bling_ligado():
    return _env("INV_BLING_ESCREVER", "1") not in ("0", "false", "nao", "não", "off")


def _zerar_inv(sku, motivo):
    """Não encontrei no inventário geral: balanço 0 no depósito. -> (ok, saldo_antes, msg)."""
    if not _escrever_bling_ligado():
        return False, None, "Escrever no Bling está desligado (INV_BLING_ESCREVER=0)."
    try:
        NA, bs = _bling()
        dep = NA._deposito()
        try:
            pid = NA._produto_id(bs, sku)
        except LookupError as e:
            return False, None, str(e)
        antes = NA._saldo_no_deposito(bs, pid, dep)
        if antes <= 0:
            return True, antes, f"No Bling já estava {antes:g}: nada foi lançado."
        corpo = {"produto": {"id": pid}, "deposito": {"id": int(dep)}, "operacao": "B", "quantidade": 0,
                 "observacoes": ("WMS inventário geral: não encontrado. " + (motivo or ""))[:250]}
        w = NA._bling_req(bs._post, "/estoques", json=corpo)
        if w.status_code not in (200, 201):
            msg = f"Bling recusou o balanço: HTTP {w.status_code} {w.text[:200]}"
            if "lote" in w.text.lower():
                msg += " (produto com controle de lote: zere no Bling, lote por lote)"
            return False, antes, msg
        depois = NA._saldo_no_deposito(bs, pid, dep)
        if abs(depois) > 1e-6:
            return False, antes, f"Balanço enviado, mas o saldo ficou {depois:g}: confira no Bling."
        return True, antes, f"Saldo {antes:g} -> 0 no depósito {dep}."
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"[:300]


def _devolver_inv(sku, qtd, motivo):
    if not _escrever_bling_ligado():
        return False, "Escrever no Bling está desligado (INV_BLING_ESCREVER=0)."
    if not qtd or qtd <= 0:
        return True, "Nada a devolver (o saldo já era zero quando zerou)."
    try:
        NA, bs = _bling()
        dep = NA._deposito()
        pid = NA._produto_id(bs, sku)
        antes = NA._saldo_no_deposito(bs, pid, dep)
        corpo = {"produto": {"id": pid}, "deposito": {"id": int(dep)}, "operacao": "E", "quantidade": float(qtd),
                 "observacoes": ("WMS inventário geral: achado, devolve o saldo zerado. " + (motivo or ""))[:250]}
        w = NA._bling_req(bs._post, "/estoques", json=corpo)
        if w.status_code not in (200, 201):
            return False, f"Bling recusou a entrada: HTTP {w.status_code} {w.text[:200]}"
        depois = NA._saldo_no_deposito(bs, pid, dep)
        return True, f"Saldo {antes:g} -> {depois:g} no depósito {dep} (+{float(qtd):g})."
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:300]


def _geral_resumo(c, g, gestor):
    gid = int(g["id"])
    tars = _geral_tarefas(c, gid)
    estr, rec = [], []
    for tid, v in tars.items():
        t = _tarefa(c, tid)
        if not t:
            continue
        ref = json.loads(t["ref"] or "{}")
        n = c.execute("SELECT COUNT(*) AS n FROM (SELECT DISTINCT porta_palet, posicao FROM inv_contagem WHERE tarefa_id=? AND rodada=1) x",
                      (tid,)).fetchone()["n"]
        item = {"tarefa_id": tid, "titulo": t["titulo"], "status": t["status"], "pego_por": t["pego_por"], "feito_por": t["feito_por"],
                "posicoes": len(ref.get("posicoes") or []), "contadas": int(n or 0), "estruturas": ref.get("estruturas") or []}
        (rec if v["tipo"] == "recontar" else estr).append(item)
    estr.sort(key=lambda x: x["tarefa_id"])
    rec.sort(key=lambda x: x["tarefa_id"])
    skus = [dict(r) for r in c.execute("SELECT * FROM inv_geral_sku WHERE geral_id=? ORDER BY nome", (gid,)).fetchall()]
    conta = {}
    for s in skus:
        conta[s["status"]] = conta.get(s["status"], 0) + 1
    agora = int(time.time())
    campos = ("sku_key", "sku", "nome", "status", "contado", "contado1", "bling", "separados", "msg", "por", "em", "trava_por", "trava_em", "div_id")
    itens = []
    for s in skus:
        if s["status"] in ("ok",) and not gestor:
            continue
        d = {k: s.get(k) for k in campos}
        if not gestor:                              # às cegas: operador nunca vê o Bling
            for k in ("contado", "contado1", "bling", "separados", "msg"):
                d.pop(k, None)
        if d.get("trava_em") and agora - int(d["trava_em"]) > GERAL_TRAVA_S:
            d["trava_por"] = None
        itens.append(d)
    return {"geral": g, "estruturas": estr, "recontagens": rec, "conta": conta, "itens": itens,
            "estruturas_feitas": sum(1 for x in estr if x["status"] == "feita"),
            "posicoes": sum(x["posicoes"] for x in estr), "posicoes_contadas": sum(min(x["contadas"], x["posicoes"]) for x in estr)}


def _geral_faltam(c, gid, eu):
    """Pop-up piscando: o que tem saldo no Bling e ninguém contou (sem o número do Bling)."""
    agora = int(time.time())
    out = []
    por_pos, por_sku = _skus_no_mapa(c)
    ML, _ = _layout()
    for r in c.execute("SELECT sku_key, sku, nome, status, trava_por, trava_em, msg FROM inv_geral_sku WHERE geral_id=? "
                       "AND status IN ('nao_contado','contar_de_novo') ORDER BY nome", (gid,)).fetchall():
        if r["msg"] == "conferindo":
            continue                                     # acabou de ser contado: o WMS está conferindo com o Bling
        trava = r["trava_por"] if r["trava_em"] and agora - int(r["trava_em"]) <= GERAL_TRAVA_S else None
        if trava and trava != eu:
            continue                                     # alguém já está procurando esse
        locais = [_rotulo(ML, a, b) if ML else f"{a}/{b}" for a, b in (por_sku.get(r["sku_key"]) or [])][:4]
        foto = next((it.get("foto") for k in (por_sku.get(r["sku_key"]) or []) for it in por_pos.get(k, [])
                     if _key(it["sku"]) == r["sku_key"] and it.get("foto")), None)
        out.append({"sku_key": r["sku_key"], "sku": r["sku"], "nome": r["nome"], "status": r["status"], "meu": trava == eu,
                    "locais": locais, "foto": foto})
    return out


# ─────────────────────────── pedido parado na Separação: por quê? (Biel 19/11 16:03) ───────────────────────────
# "Tem produto em separação ainda ativo e eu tenho que perguntar por quê. Quero resolver isso." O WMS cobra sozinho:
# pedido começado (tem bip) e sem terminar há PARADA_HORAS (2 h), ou qualquer pendente/parcial depois do corte do dia
# (PARADA_CORTE, 15:30 Brasília). Cobra quem deu o último bip; sem bip, qualquer operador (todos menos admin) até alguém
# responder. Respostas: nao_achei (vira Não achado: zera no Bling como na separação), falta_produto (comprando),
# cancelado (arquiva o pedido), termino_agora (30 min e cobra de novo), outro (texto). O admin vê tudo em
# GET /separacao/paradas (Separação e Início).
PARADA_HORAS = lambda: float(_env("SEP_PARADA_HORAS", "2") or 2)
PARADA_CORTE = lambda: _env("SEP_PARADA_CORTE", "15:30")
PARADA_LIGADA = lambda: _env("SEP_PARADA", "1") not in ("0", "false", "nao", "não", "off")
_parada_ultimo = [0]
_cols_audit = {}


def _tabela_parada(c, pk):
    c.execute(f"""CREATE TABLE IF NOT EXISTS sep_parada (id {pk}, ml_id TEXT NOT NULL, cliente TEXT, origem TEXT, status TEXT,
        itens INTEGER, bipados INTEGER, ultimo_bip BIGINT, cobrado_de TEXT, motivo_cobranca TEXT, detectado_em BIGINT,
        respondido_por TEXT, respondido_em BIGINT, resposta TEXT, texto TEXT, sku TEXT, prazo_em BIGINT, feito TEXT, dia TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sep_parada_ml ON sep_parada (ml_id, detectado_em)")


def _audit_cols(c):
    """Colunas da audit_log (o arquivo dela só existe no servidor): quem bipou e quando."""
    if _cols_audit:
        return _cols_audit
    try:
        rows = c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='audit_log'").fetchall()
        cols = {r["column_name"] for r in rows}
    except Exception:
        try:
            cols = {x[1] for x in c.execute("PRAGMA table_info(audit_log)").fetchall()}
        except Exception:
            cols = set()
    pega = lambda lst: next((x for x in lst if x in cols), None)
    _cols_audit.update({"user": pega(["user_id", "usuario_id", "username", "usuario"]),
                        "quando": pega(["created_at", "criado_em", "timestamp", "em", "data", "ts"]),
                        "entity": pega(["entity_id", "entidade_id", "ml_id"]), "action": pega(["action", "acao"]),
                        "ok": bool(cols)})
    return _cols_audit


def _ultimos_bips(c, ml_ids):
    """{ml_id: (username, ts)} do último bip de separação de cada pedido (audit_log action 'bipar')."""
    out = {}
    if not ml_ids:
        return out
    a = _audit_cols(c)
    if not a.get("ok") or not a.get("user") or not a.get("quando") or not a.get("entity"):
        return out
    q, u, e, ac = a["quando"], a["user"], a["entity"], a["action"]
    try:
        ids = list(ml_ids)
        for i in range(0, len(ids), 200):
            g = ids[i:i + 200]
            sql = (f"SELECT {e} AS ml, {u} AS usr, {q} AS q FROM audit_log WHERE {e} IN ({','.join('?' * len(g))})"
                   + (f" AND {ac} IN ('bipar','bipar_lote','separar')" if ac else "") + f" ORDER BY {q} DESC")
            for r in c.execute(sql, tuple(g)).fetchall():
                if r["ml"] in out:
                    continue
                ts = r["q"]
                try:
                    import calendar
                    import datetime as _dt
                    if isinstance(ts, str):
                        ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
                    if isinstance(ts, _dt.datetime):
                        # sem fuso (timestamp without time zone do Postgres / texto): o servidor grava em UTC
                        ts = int(ts.timestamp()) if ts.tzinfo else calendar.timegm(ts.timetuple())
                    else:
                        ts = int(ts or 0)
                        if ts > 10 ** 11:
                            ts //= 1000
                except Exception:
                    ts = 0
                out[r["ml"]] = (r["usr"], ts)
        # user_id -> username
        nums = {v[0] for v in out.values() if isinstance(v[0], int) or (isinstance(v[0], str) and v[0].isdigit())}
        if nums:
            nome = {}
            for r in c.execute(f"SELECT id, username FROM users WHERE id IN ({','.join('?' * len(nums))})", tuple(int(x) for x in nums)).fetchall():
                nome[int(r["id"])] = r["username"]
            out = {k: (nome.get(int(v[0]), str(v[0])) if (isinstance(v[0], int) or str(v[0]).isdigit()) else v[0], v[1]) for k, v in out.items()}
    except Exception:
        return {}
    return out


def _passou_corte():
    try:
        h, m = PARADA_CORTE().split(":")
        t = _br()
        return (t.tm_hour, t.tm_min) >= (int(h), int(m))
    except Exception:
        return False


def _detectar_paradas():
    """Roda no máximo 1x por minuto: acha pedido parado e abre a cobrança (uma aberta por pedido)."""
    if not PARADA_LIGADA():
        return
    agora = int(time.time())
    if agora - _parada_ultimo[0] < 60:
        return
    _parada_ultimo[0] = agora
    _tabelas()
    with db_session() as c:
        rows = [dict(r) for r in c.execute("""SELECT p.id, p.ml_id, p.cliente_nome, p.status, p.logistic_type,
                   COALESCE(SUM(pp.quantidade),0) AS itens, COALESCE(SUM(pp.quantidade_bipada),0) AS bipados
                   FROM pedidos p LEFT JOIN pedido_produtos pp ON pp.pedido_id = p.id
                   WHERE p.status IN ('PENDENTE','PARCIAL') GROUP BY p.id, p.ml_id, p.cliente_nome, p.status, p.logistic_type""").fetchall()]
        if not rows:
            return
        abertas = {r["ml_id"]: dict(r) for r in c.execute(
            "SELECT * FROM sep_parada WHERE resposta IS NULL OR (resposta='termino_agora' AND prazo_em > ?) "
            "OR (resposta IS NOT NULL AND respondido_em > ?)", (agora, agora - 12 * 3600)).fetchall()}
        bips = _ultimos_bips(c, [r["ml_id"] for r in rows])
        corte = _passou_corte()
        lim = int(PARADA_HORAS() * 3600)
        for r in rows:
            if r["ml_id"] in abertas:
                continue
            quem, ts = bips.get(r["ml_id"], (None, 0))
            comecado = int(r["bipados"] or 0) > 0
            motivo = None
            if comecado and ts and agora - ts >= lim:
                motivo = f"começado há {max(1, (agora - ts) // 3600)} h e não terminou"
            elif comecado and not ts and lim <= 3600 * 2:
                pass                                      # sem hora do bip: só cobra pelo corte
            if not motivo and corte:
                motivo = f"passou das {PARADA_CORTE()} e ainda está na Separação"
            if not motivo:
                continue
            c.execute("INSERT INTO sep_parada (ml_id, cliente, origem, status, itens, bipados, ultimo_bip, cobrado_de, motivo_cobranca, detectado_em, dia) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (r["ml_id"], r["cliente_nome"], r["logistic_type"], r["status"], int(r["itens"] or 0), int(r["bipados"] or 0),
                       ts or None, (quem or "") if comecado else "", motivo, agora, _hoje()))


def _paradas_para(c, eu, gestor=False):
    agora = int(time.time())
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM sep_parada WHERE resposta IS NULL OR (resposta='termino_agora' AND prazo_em <= ?) ORDER BY detectado_em", (agora,)).fetchall()]
    # ainda está parado? (pode ter terminado/arquivado depois da cobrança)
    out = []
    for p in rows:
        st = c.execute("SELECT status FROM pedidos WHERE ml_id=?", (p["ml_id"],)).fetchone()
        if not st or st["status"] not in ("PENDENTE", "PARCIAL"):
            c.execute("UPDATE sep_parada SET resposta=COALESCE(resposta,'resolvido'), respondido_em=COALESCE(respondido_em, ?), feito=COALESCE(feito, ?) WHERE id=?",
                      (agora, f"pedido saiu da Separação ({st['status'] if st else 'apagado'})", p["id"]))
            continue
        if p["resposta"] == "termino_agora":           # prazo venceu: cobra de novo
            p["motivo_cobranca"] = (p["motivo_cobranca"] or "") + " · disse que ia terminar e não terminou"
            c.execute("UPDATE sep_parada SET resposta=NULL, prazo_em=NULL, motivo_cobranca=? WHERE id=?", (p["motivo_cobranca"], p["id"]))
            p["resposta"] = None
        if gestor or not p["cobrado_de"] or p["cobrado_de"] == eu:
            out.append({k: p.get(k) for k in ("id", "ml_id", "cliente", "origem", "status", "itens", "bipados", "ultimo_bip", "cobrado_de",
                                             "motivo_cobranca", "detectado_em")})
    return out


# ─────────────────────────── pessoas ───────────────────────────
def _pessoas(c):
    LOGIN = ["username", "usuario", "login", "user_name"]
    NOME = ["full_name", "nome", "nome_completo", "name"]
    ROLE = ["role", "papel", "perfil"]
    ATIVO = ["ativo", "active", "is_active"]
    PASS = ["password_hash", "senha_hash", "hashed_password", "pwd_hash", "password", "senha"]
    try:
        rows = c.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'").fetchall()
        tabs = {}
        for r in rows:
            tabs.setdefault(r["table_name"], []).append(r["column_name"])
    except Exception:
        tabs = {}
        for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            tabs[r["name"]] = [x[1] for x in c.execute(f"PRAGMA table_info({r['name']})").fetchall()]
    cand = [(t, cols) for t, cols in tabs.items() if any(p in cols for p in PASS) and any(l in cols for l in LOGIN)
            and not t.startswith(("bkp_", "backup"))]
    if not cand:
        return []
    cand.sort(key=lambda x: (x[0] not in ("users", "usuarios", "user"), x[0]))
    t, cols = cand[0]
    pega = lambda lst: next((x for x in lst if x in cols), None)
    lg, nm, rl, at = pega(LOGIN), pega(NOME), pega(ROLE), pega(ATIVO)
    campos = ", ".join(x for x in (lg, nm, rl, at) if x)
    out = []
    for r in c.execute(f"SELECT {campos} FROM {t}").fetchall():
        if at and r[at] in (0, False, "0", "false"):
            continue
        out.append({"usuario": r[lg], "nome": (r[nm] if nm else None) or r[lg], "papel": (r[rl] if rl else "") or ""})
    out.sort(key=lambda x: str(x["nome"]).lower())
    return out


# ─────────────────────────── rotas ───────────────────────────
def registrar(bp):
    # cadastro só por convite (convites_routes.py): vem junto com as tarefas para não mexer de novo no mapa_routes.py
    try:
        import convites_routes
        convites_routes.registrar(bp)
    except Exception as e:                     # sem o arquivo: o cadastro continua como era
        import logging
        logging.getLogger("gunicorn.error").warning("[convites] não carregou: %s", e)

    @bp.get("/tarefas")
    @login_required
    def tarefas_listar():
        _tabelas()
        try:
            _gerar_hoje()
        except Exception:
            pass
        eu = _quem()
        todas = request.args.get("escopo") == "todas" and _gestor()
        ini = int(time.time()) - 20 * 3600
        with db_session() as c:
            if todas:
                rows = c.execute("SELECT * FROM tarefas WHERE status IN ('aberta','andamento','conferindo','recontar') "
                                 "OR (status IN ('feita','cancelada') AND COALESCE(feito_em, criado_em) >= ?) ORDER BY criado_em DESC LIMIT 300",
                                 (ini,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM tarefas WHERE (status IN ('aberta') AND (para = ? OR para = '')) "
                                 "OR (status IN ('andamento','conferindo','recontar') AND (pego_por = ? OR (para = ? AND pego_por IS NULL))) "
                                 "OR (status = 'feita' AND (pego_por = ? OR feito_por = ? OR para = ?) AND feito_em >= ?) "
                                 "ORDER BY criado_em DESC LIMIT 200", (eu, eu, eu, eu, eu, eu, ini)).fetchall()
            vistas = {int(r["tarefa_id"]) for r in c.execute("SELECT tarefa_id FROM tarefa_vista WHERE usuario=?", (eu,)).fetchall()}
            # inventário em andamento: quantas posições já foram contadas na rodada de agora (Painel TV e Início)
            ids_inv = [int(r["id"]) for r in rows if r["tipo"] == "inventario" and r["status"] in ("andamento", "recontar", "conferindo")]
            contadas = {}
            for tid in ids_inv:
                t = next(dict(r) for r in rows if int(r["id"]) == tid)
                try:
                    rod = int(json.loads(t.get("ref") or "{}").get("rodada", 1))
                except Exception:
                    rod = 1
                n = c.execute("SELECT COUNT(*) AS n FROM (SELECT DISTINCT porta_palet, posicao FROM inv_contagem WHERE tarefa_id=? AND rodada=?) x",
                              (tid, rod)).fetchone()["n"]
                contadas[tid] = int(n or 0)
        lista = [_fmt(dict(r), vistas) for r in rows]
        for t in lista:
            if t["id"] in contadas:
                t["inventario"]["contadas"] = contadas[t["id"]]
        if not todas and _role() == "admin" and _env("INV_AVISO_ADMIN", "0") != "1":
            lista = [t for t in lista if not (t["tipo"] == "inventario" and t["status"] == "aberta")]
        return jsonify({"tarefas": lista, "eu": eu, "gestor": _gestor(), "agora": int(time.time())})

    @bp.post("/tarefas")
    @login_required
    def tarefas_criar():
        if not _gestor():
            return jsonify({"error": "Só admin ou supervisor passa tarefa."}), 403
        _tabelas()
        d = request.get_json(silent=True) or {}
        titulo = str(d.get("titulo") or "").strip()
        if len(titulo) < 3:
            return jsonify({"error": "Escreva o que é para fazer."}), 400
        prazo = d.get("prazo")
        try:
            prazo = int(prazo) if prazo else None
        except (TypeError, ValueError):
            prazo = None
        with db_session() as c:
            tid = _ins(c, "INSERT INTO tarefas (tipo, titulo, descricao, local, para, criado_por, criado_em, prazo_em, status) "
                          "VALUES ('livre', ?, ?, ?, ?, ?, ?, ?, 'aberta')",
                       (titulo[:200], str(d.get("descricao") or "").strip()[:1000] or None, str(d.get("local") or "").strip()[:60] or None,
                        str(d.get("para") or "").strip()[:80], _quem(), int(time.time()), prazo))
            c.execute("INSERT INTO tarefa_vista (tarefa_id, usuario, em) VALUES (?,?,?)", (tid, _quem(), int(time.time())))
            t = _tarefa(c, tid)
        return jsonify({"tarefa": _fmt(t)})

    def _mudar(tid, fn):
        _tabelas()
        with db_session() as c:
            c.execute("UPDATE tarefas SET id = id WHERE id = ?", (tid,))          # trava a linha (dois tocando junto)
            t = _tarefa(c, tid)
            if not t:
                return jsonify({"error": "Tarefa não encontrada."}), 404
            erro = fn(c, t)
            if erro:
                return jsonify({"error": erro}), 409
            return jsonify({"tarefa": _fmt(_tarefa(c, tid))})

    @bp.post("/tarefas/<int:tid>/pegar")
    @login_required
    def tarefas_pegar(tid):
        eu = _quem()

        def fn(c, t):
            if t["status"] in ("andamento", "conferindo", "recontar") and t["pego_por"] == eu:
                return None
            if t["status"] != "aberta":
                return f"Já está com {t['pego_por']}." if t["pego_por"] else "Essa tarefa não está mais aberta."
            if t["para"] and t["para"] != eu and not _gestor():
                return "Essa tarefa é de outra pessoa."
            c.execute("UPDATE tarefas SET status='andamento', pego_por=?, pego_em=? WHERE id=? AND status='aberta'", (eu, int(time.time()), t["id"]))
            c.execute("DELETE FROM tarefa_vista WHERE tarefa_id=? AND usuario=?", (t["id"], eu))
            c.execute("INSERT INTO tarefa_vista (tarefa_id, usuario, em) VALUES (?,?,?)", (t["id"], eu, int(time.time())))
            return None
        return _mudar(tid, fn)

    @bp.post("/tarefas/<int:tid>/soltar")
    @login_required
    def tarefas_soltar(tid):
        eu = _quem()

        def fn(c, t):
            if t["pego_por"] != eu and not _gestor():
                return "Só quem pegou (ou admin/supervisor) devolve."
            if t["status"] not in ("andamento",):
                return "Agora não dá para devolver essa tarefa."
            c.execute("UPDATE tarefas SET status='aberta', pego_por=NULL, pego_em=NULL WHERE id=?", (t["id"],))
            return None
        return _mudar(tid, fn)

    @bp.post("/tarefas/<int:tid>/feita")
    @login_required
    def tarefas_feita(tid):
        eu = _quem()
        obs = str((request.get_json(silent=True) or {}).get("obs") or "").strip()[:500]

        def fn(c, t):
            if t["tipo"] == "inventario":
                return "Inventário termina pelo Enviar contagem."
            if t["status"] not in ("aberta", "andamento"):
                return "Essa tarefa não está mais aberta."
            if t["para"] and t["para"] != eu and t["pego_por"] != eu and not _gestor():
                return "Essa tarefa é de outra pessoa."
            c.execute("UPDATE tarefas SET status='feita', feito_por=?, feito_em=?, obs_fim=?, pego_por=COALESCE(pego_por, ?) WHERE id=?",
                      (eu, int(time.time()), obs or None, eu, t["id"]))
            return None
        return _mudar(tid, fn)

    @bp.post("/tarefas/<int:tid>/cancelar")
    @login_required
    def tarefas_cancelar(tid):
        def fn(c, t):
            if not _gestor():
                return "Só admin ou supervisor cancela."
            if t["status"] in ("feita", "cancelada"):
                return "Essa tarefa já foi encerrada."
            c.execute("UPDATE tarefas SET status='cancelada', feito_por=?, feito_em=? WHERE id=?", (_quem(), int(time.time()), t["id"]))
            return None
        return _mudar(tid, fn)

    @bp.post("/tarefas/<int:tid>/vista")
    @login_required
    def tarefas_vista(tid):
        _tabelas()
        with db_session() as c:
            c.execute("DELETE FROM tarefa_vista WHERE tarefa_id=? AND usuario=?", (tid, _quem()))
            c.execute("INSERT INTO tarefa_vista (tarefa_id, usuario, em) VALUES (?,?,?)", (tid, _quem(), int(time.time())))
        return jsonify({"ok": True})

    @bp.get("/tarefas/aviso")
    @login_required
    def tarefas_aviso():
        _tabelas()
        eu, papel = _quem(), _role()
        inv = None
        try:
            t = _gerar_hoje()
        except Exception:
            t = None
        with db_session() as c:
            if t:
                t = _tarefa(c, t["id"])
            libera = False
            if t:
                try:
                    libera = _passou_da_hora() or bool(json.loads(t["ref"] or "{}").get("forcado"))
                except Exception:
                    libera = _passou_da_hora()
            if t and t["tipo"] == "inventario" and (papel != "admin" or _env("INV_AVISO_ADMIN", "0") == "1"):
                if t["status"] == "aberta" and libera:
                    inv = {"tarefa": _fmt(t), "acao": "comecar"}
                elif t["status"] in ("andamento", "recontar") and t["pego_por"] == eu:
                    inv = {"tarefa": _fmt(t), "acao": "recontar" if t["status"] == "recontar" else "continuar"}
            # inventário geral: a contagem de quem já pegou, senão a próxima livre (recontagem primeiro)
            geral = _geral_aberto(c)
            faltam = None
            if geral and not inv and (papel != "admin" or _env("INV_AVISO_ADMIN", "0") == "1"):
                r = c.execute("SELECT t.* FROM tarefas t JOIN inv_geral_tarefa x ON x.tarefa_id = t.id WHERE x.geral_id=? "
                              "AND t.status IN ('andamento','recontar') AND t.pego_por=? ORDER BY t.id LIMIT 1", (geral["id"], eu)).fetchone()
                if r:
                    inv = {"tarefa": _fmt(dict(r)), "acao": "continuar"}
                else:
                    r = c.execute("SELECT t.* FROM tarefas t JOIN inv_geral_tarefa x ON x.tarefa_id = t.id WHERE x.geral_id=? "
                                  "AND t.status='aberta' ORDER BY CASE WHEN x.tipo='recontar' THEN 0 ELSE 1 END, t.id LIMIT 1",
                                  (geral["id"],)).fetchone()
                    if r:
                        inv = {"tarefa": _fmt(dict(r)), "acao": "comecar"}
            if geral and geral["status"] == "procurando" and (papel != "admin" or _env("INV_AVISO_ADMIN", "0") == "1"):
                itens = _geral_faltam(c, int(geral["id"]), eu)
                if itens:
                    faltam = {"geral_id": int(geral["id"]), "itens": itens[:40], "n": len(itens)}
            # recontagem pedida pelo admin (tarefa só de alguns produtos)
            if not inv and papel != "admin":
                r = c.execute("SELECT * FROM tarefas WHERE tipo='inventario' AND status='aberta' AND ref LIKE ? ORDER BY criado_em LIMIT 1",
                              ('%"so_skus"%',)).fetchone()
                if r:
                    inv = {"tarefa": _fmt(dict(r)), "acao": "comecar"}
            vistas = {int(r["tarefa_id"]) for r in c.execute("SELECT tarefa_id FROM tarefa_vista WHERE usuario=?", (eu,)).fetchall()}
            novas = [_fmt(dict(r)) for r in c.execute(
                "SELECT * FROM tarefas WHERE tipo='livre' AND status='aberta' AND (para=? OR para='') AND COALESCE(criado_por,'') <> ? "
                "ORDER BY criado_em DESC LIMIT 20", (eu, eu)).fetchall() if int(r["id"]) not in vistas][:5]
            div = None
            if papel == "admin":
                n = c.execute("SELECT COUNT(*) AS n FROM inv_divergencia WHERE status='aberta'").fetchone()["n"]
                nv = c.execute("SELECT COUNT(*) AS n FROM inv_divergencia WHERE status='aberta' AND visto=0").fetchone()["n"]
                div = {"abertas": int(n or 0), "novas": int(nv or 0)}
        paradas = []
        try:
            _detectar_paradas()
            if papel != "admin":
                with db_session() as c:
                    paradas = _paradas_para(c, eu)
        except Exception:
            paradas = []
        return jsonify({"inventario": inv, "novas": novas, "divergencias": div, "intervalo_min": int(_env("INV_AVISO_MIN", "15") or 15),
                        "geral_faltam": faltam, "sep_paradas": paradas,
                        "hora_inventario": "%02d:%02d" % _hora_inv(), "agora": int(time.time())})

    @bp.get("/tarefas/pessoas")
    @login_required
    def tarefas_pessoas():
        if not _gestor():
            return jsonify({"error": "Só admin ou supervisor."}), 403
        with db_session() as c:
            try:
                return jsonify({"pessoas": _pessoas(c)})
            except Exception as e:
                return jsonify({"pessoas": [], "erro": str(e)[:200]})

    @bp.get("/inicio/resumo")
    @login_required
    def inicio_resumo():
        _tabelas()
        out = {"hoje": _hoje(), "agora": int(time.time())}
        # cada parte na sua conexão: no Postgres um erro (tabela que ainda não existe) aborta a transação inteira
        try:
            with db_session() as c:
                r = c.execute("""SELECT COALESCE(SUM(pp.quantidade),0) AS t, COALESCE(SUM(pp.quantidade_bipada),0) AS b, COUNT(DISTINCT p.id) AS n
                                   FROM pedidos p JOIN pedido_produtos pp ON pp.pedido_id = p.id WHERE p.status = 'PENDENTE'""").fetchone()
                n_sep = c.execute("SELECT COUNT(*) AS n FROM pedidos WHERE status = 'SEPARADO'").fetchone()["n"]
            out["separacao"] = {"itens": int(r["t"] or 0), "bipados": int(r["b"] or 0), "pedidos": int(r["n"] or 0),
                                "esperando_expedicao": int(n_sep or 0)}
        except Exception as e:
            out["separacao"] = {"erro": str(e)[:120]}
        try:
            with db_session() as c:
                rows = [dict(x) for x in c.execute("SELECT id, nome, frete, data_envio, status FROM full_remessas "
                                                   "WHERE status IN ('aberta','separando','separada') ORDER BY id").fetchall()]
                full = []
                for x in rows:
                    s_ = c.execute("SELECT COALESCE(SUM(qtd),0) AS q, COALESCE(SUM(separado),0) AS s, COALESCE(SUM(etiquetado),0) AS e "
                                   "FROM full_itens WHERE remessa_id=?", (x["id"],)).fetchone()
                    full.append({**x, "qtd": int(s_["q"] or 0), "separado": int(s_["s"] or 0), "etiquetado": int(s_["e"] or 0)})
            out["full"] = full
        except Exception:
            out["full"] = []
        try:
            with db_session() as c:
                out["inventario"] = _plano(c)
        except Exception as e:
            out["inventario"] = {"erro": str(e)[:120]}
        if _gestor():
            try:
                with db_session() as c:
                    out["divergencias"] = int(c.execute("SELECT COUNT(*) AS n FROM inv_divergencia WHERE status='aberta'").fetchone()["n"] or 0)
            except Exception:
                out["divergencias"] = 0
        return jsonify(out)

    @bp.get("/inventario/mes")
    @login_required
    def inventario_mes():
        _tabelas()
        try:
            _gerar_hoje()
        except Exception:
            pass
        with db_session() as c:
            return jsonify(_plano(c, request.args.get("mes") or None))

    @bp.post("/inventario/gerar")
    @login_required
    def inventario_gerar():
        if not _gestor():
            return jsonify({"error": "Só admin ou supervisor."}), 403
        t = _gerar_hoje(forcar=True)
        with db_session() as c:
            return jsonify({"tarefa": _fmt(t) if t else None, "plano": _plano(c)})

    @bp.get("/inventario/tarefa/<int:tid>")
    @login_required
    def inventario_tarefa(tid):
        _tabelas()
        with db_session() as c:
            t = _tarefa(c, tid)
            if not t or t["tipo"] != "inventario":
                return jsonify({"error": "Tarefa de inventário não encontrada."}), 404
            ref = json.loads(t["ref"] or "{}")
            rod = int(ref.get("rodada", 1))
            so = set(ref.get("recontar") or []) if rod > 1 else (set(ref.get("so_skus") or []) or None)
            por_pos, _ = _skus_no_mapa(c)
            cont = [dict(r) for r in c.execute("SELECT * FROM inv_contagem WHERE tarefa_id=? AND rodada=? ORDER BY id", (tid, rod)).fetchall()]
            res = [dict(r) for r in c.execute("SELECT sku, sku_key, nome, status FROM inv_sku WHERE tarefa_id=? ORDER BY id", (tid,)).fetchall()]
        _, ML = _estruturas_em_ordem() if True else (None, None)
        posicoes = []
        for pp, p in ref.get("posicoes") or []:
            prods = por_pos.get((pp, p), [])
            if so is not None:
                prods = [x for x in prods if _key(x["sku"]) in so]
                if not prods and not any(int(l["porta_palet"]) == pp and int(l["posicao"]) == p and l["sku_key"] in so for l in cont):
                    continue
            minhas = [l for l in cont if int(l["porta_palet"]) == pp and int(l["posicao"]) == p]
            end = None
            try:
                end = ML.endereco(pp, p) if ML else None
            except Exception:
                pass
            posicoes.append({"porta_palet": pp, "posicao": p, "rotulo": _rotulo(ML, pp, p) if ML else f"{pp}/{p}", "endereco": end,
                             "produtos": prods, "feita": bool(minhas), "vazia": any(l["vazia"] for l in minhas),
                             "contado": {l["sku_key"]: l["qtd"] for l in minhas if not l["vazia"]},
                             "achados": [{"sku": l["sku"], "nome": l["nome"], "qtd": l["qtd"]} for l in minhas if l["achado"]]})
        # o operador nunca vê o número do Bling; só sabe o que ficou ok e o que precisa recontar
        ult = {}
        for r in res:                              # vale a última rodada de cada produto
            ult[r["sku_key"]] = r["status"]
        resumo = {"ok": sum(1 for v in ult.values() if v == "ok"), "recontar": sum(1 for v in ult.values() if v == "recontar"),
                  "divergentes": sum(1 for v in ult.values() if v == "divergente")}
        return jsonify({"tarefa": _fmt(t), "rodada": rod, "posicoes": posicoes, "conferindo": tid in _conferindo or t["status"] == "conferindo",
                        "resultado": resumo, "eu": _quem(), "meu": t["pego_por"] == _quem()})

    @bp.post("/inventario/tarefa/<int:tid>/contar")
    @login_required
    def inventario_contar(tid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        try:
            pp, p = int(d.get("porta_palet")), int(d.get("posicao"))
        except (TypeError, ValueError):
            return jsonify({"error": "Posição inválida."}), 400
        eu = _quem()
        with db_session() as c:
            c.execute("UPDATE tarefas SET id = id WHERE id = ?", (tid,))
            t = _tarefa(c, tid)
            if not t or t["tipo"] != "inventario":
                return jsonify({"error": "Tarefa não encontrada."}), 404
            if t["status"] == "aberta":
                c.execute("UPDATE tarefas SET status='andamento', pego_por=?, pego_em=? WHERE id=?", (eu, int(time.time()), tid))
                t = _tarefa(c, tid)
            if t["status"] not in ("andamento", "recontar"):
                return jsonify({"error": "Essa contagem não está aberta."}), 409
            if t["pego_por"] != eu and not _gestor():
                return jsonify({"error": f"Essa contagem está com {t['pego_por']}."}), 409
            ref = json.loads(t["ref"] or "{}")
            if [pp, p] not in (ref.get("posicoes") or []):
                return jsonify({"error": "Essa posição não é desta contagem."}), 400
            rod = int(ref.get("rodada", 1))
            c.execute("DELETE FROM inv_contagem WHERE tarefa_id=? AND rodada=? AND porta_palet=? AND posicao=?", (tid, rod, pp, p))
            agora = int(time.time())
            if d.get("vazia"):
                c.execute("INSERT INTO inv_contagem (tarefa_id, rodada, porta_palet, posicao, vazia, por, em) VALUES (?,?,?,?,1,?,?)",
                          (tid, rod, pp, p, eu, agora))
            else:
                itens = d.get("itens") or []
                if not itens:
                    return jsonify({"error": "Digite quanto tem de cada produto (0 se não tem) ou marque posição vazia."}), 400
                for it in itens:
                    sku = str(it.get("sku") or "").strip()
                    try:
                        q = float(it.get("qtd"))
                    except (TypeError, ValueError):
                        return jsonify({"error": f"Quantidade inválida em {sku}."}), 400
                    if not sku or q < 0 or q > 100000:
                        return jsonify({"error": f"Quantidade inválida em {sku or 'produto'}."}), 400
                    nome = str(it.get("nome") or "").strip()[:200] or None
                    c.execute("INSERT INTO inv_contagem (tarefa_id, rodada, porta_palet, posicao, sku, sku_key, nome, qtd, achado, por, em) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (tid, rod, pp, p, sku[:64], _key(sku), nome, q, 1 if it.get("achado") else 0, eu, agora))
        return jsonify({"ok": True})

    @bp.post("/inventario/tarefa/<int:tid>/enviar")
    @login_required
    def inventario_enviar(tid):
        _tabelas()
        eu = _quem()
        with db_session() as c:
            c.execute("UPDATE tarefas SET id = id WHERE id = ?", (tid,))
            t = _tarefa(c, tid)
            if not t or t["tipo"] != "inventario":
                return jsonify({"error": "Tarefa não encontrada."}), 404
            if t["status"] not in ("andamento", "recontar"):
                return jsonify({"error": "Essa contagem não está aberta."}), 409
            if t["pego_por"] != eu and not _gestor():
                return jsonify({"error": f"Essa contagem está com {t['pego_por']}."}), 409
            ref = json.loads(t["ref"] or "{}")
            rod = int(ref.get("rodada", 1))
            feitas = {(int(r["porta_palet"]), int(r["posicao"])) for r in
                      c.execute("SELECT porta_palet, posicao FROM inv_contagem WHERE tarefa_id=? AND rodada=?", (tid, rod)).fetchall()}
            so = set(ref.get("recontar") or []) if rod > 1 else None
            por_pos, _ = _skus_no_mapa(c)
            faltam = []
            for pp, p in ref.get("posicoes") or []:
                if so is not None and not any(_key(x["sku"]) in so for x in por_pos.get((pp, p), [])):
                    continue
                if (pp, p) not in feitas:
                    faltam.append([pp, p])
            if faltam:
                return jsonify({"error": f"Faltam {len(faltam)} posição(ões) para contar.", "faltam": faltam}), 400
            do_geral = _geral_de_tarefa(c, tid) is not None
            c.execute("UPDATE tarefas SET status='conferindo' WHERE id=?", (tid,))
        _conferindo.add(tid)
        threading.Thread(target=_enviar_geral if do_geral else _conferir, args=(tid,), daemon=True).start()
        return jsonify({"ok": True, "conferindo": True})

    @bp.get("/inventario/divergencias")
    @login_required
    def inventario_divergencias():
        if not _gestor():
            return jsonify({"error": "Só admin ou supervisor."}), 403
        _tabelas()
        st = request.args.get("status") or "aberta"
        with db_session() as c:
            if st == "todas":
                rows = c.execute("SELECT * FROM inv_divergencia ORDER BY criado_em DESC LIMIT 200").fetchall()
            else:
                rows = c.execute("SELECT * FROM inv_divergencia WHERE status=? ORDER BY criado_em DESC LIMIT 200", (st,)).fetchall()
            if _admin():
                c.execute("UPDATE inv_divergencia SET visto=1 WHERE status='aberta' AND visto=0")
            ML, _ = _layout()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["locais"] = [{"porta_palet": a, "posicao": b, "qtd": q, "rotulo": _rotulo(ML, a, b) if ML else f"{a}/{b}"}
                               for a, b, q in json.loads(d.get("locais") or "[]")]
            except Exception:
                d["locais"] = []
            out.append(d)
        return jsonify({"divergencias": out, "pode_decidir": _admin()})

    @bp.post("/inventario/divergencias/<int:did>/decidir")
    @login_required
    def inventario_decidir(did):
        _tabelas()
        d = request.get_json(silent=True) or {}
        acao = str(d.get("acao") or "")
        if acao != "reler" and not _admin():
            return jsonify({"error": "Só o admin decide a divergência."}), 403
        if acao == "reler" and not _gestor():
            return jsonify({"error": "Só admin ou supervisor."}), 403
        with db_session() as c:
            c.execute("UPDATE inv_divergencia SET id = id WHERE id = ?", (did,))
            v = c.execute("SELECT * FROM inv_divergencia WHERE id=?", (did,)).fetchone()
            if not v:
                return jsonify({"error": "Divergência não encontrada."}), 404
            v = dict(v)
            if v["status"] != "aberta":
                return jsonify({"error": f"Essa divergência já foi decidida ({v['status']}) por {v.get('decidido_por') or '?'}."}), 409
        agora, eu = int(time.time()), _quem()
        if acao == "manter":
            with db_session() as c:
                c.execute("UPDATE inv_divergencia SET status='mantida', decidido_por=?, decidido_em=? WHERE id=? AND status='aberta'", (eu, agora, did))
            return jsonify({"ok": True, "status": "mantida"})
        if acao == "reler":
            saldo, _, erro = _saldo_bling(v["sku"])
            if erro:
                return jsonify({"error": f"Não li o Bling: {erro}"}), 502
            with db_session() as c:
                if abs(float(v["contado"] or 0) - saldo) < 1e-6:
                    c.execute("UPDATE inv_divergencia SET status='resolvida', bling=?, decidido_por=?, decidido_em=?, bling_msg=? WHERE id=?",
                              (saldo, eu, agora, f"Bling agora está {saldo:g}: bate com a contagem.", did))
                    return jsonify({"ok": True, "status": "resolvida", "bling": saldo})
                c.execute("UPDATE inv_divergencia SET bling=?, bling_msg=NULL WHERE id=?", (saldo, did))
            return jsonify({"ok": True, "status": "aberta", "bling": saldo})
        if acao == "recontar":
            with db_session() as c:
                _, por_sku = _skus_no_mapa(c)
                pos = por_sku.get(v["sku_key"]) or []
                try:
                    pos = pos or [(int(a), int(b)) for a, b, _ in json.loads(v.get("locais") or "[]")]
                except Exception:
                    pass
                if not pos:
                    return jsonify({"error": "Esse produto não tem posição no mapa. Cadastre no Mapa antes de recontar."}), 400
                ref = {"mes": v.get("mes") or _mes(), "dia": _hoje(), "posicoes": [list(k) for k in pos], "estruturas": [], "rodada": 1,
                       "so_skus": [v["sku_key"]], "divergencia": did}
                tid = _ins(c, "INSERT INTO tarefas (tipo, titulo, descricao, para, criado_por, criado_em, prazo_em, status, ref) "
                              "VALUES ('inventario', ?, ?, ?, ?, ?, ?, 'aberta', ?)",
                           (f"Recontar: {v['nome'] or v['sku']}"[:200], f"{len(pos)} posição(ões). O admin pediu para contar de novo.",
                            str(d.get("para") or ""), eu, agora, _fim_do_dia_ts(), json.dumps(ref)))
                c.execute("UPDATE inv_divergencia SET status='recontar', decidido_por=?, decidido_em=? WHERE id=?", (eu, agora, did))
            return jsonify({"ok": True, "status": "recontar", "tarefa_id": tid})
        if acao == "substituir":
            try:
                valor = float(d.get("valor"))
            except (TypeError, ValueError):
                return jsonify({"error": "Valor inválido."}), 400
            if valor < 0 or valor > 1000000:
                return jsonify({"error": "Valor inválido."}), 400
            if _env("INV_BLING_ESCREVER", "1") in ("0", "false", "nao", "não", "off"):
                return jsonify({"error": "Escrever no Bling está desligado (INV_BLING_ESCREVER=0)."}), 409
            try:
                NA, bs = _bling()
                dep = NA._deposito()
                pid = NA._produto_id(bs, v["sku"])
                antes = NA._saldo_no_deposito(bs, pid, dep)
                corpo = {"produto": {"id": pid}, "deposito": {"id": int(dep)}, "operacao": "B", "quantidade": valor,
                         "observacoes": f"WMS inventário: contado {float(v['contado'] or 0):g}, Bling {antes:g}; ajustado por {eu}."[:250]}
                w = NA._bling_req(bs._post, "/estoques", json=corpo)
                if w.status_code not in (200, 201):
                    msg = f"Bling recusou o balanço: HTTP {w.status_code} {w.text[:200]}"
                    if "lote" in w.text.lower():
                        msg += " (produto com controle de lote: ajuste esse no Bling, lote por lote)"
                    with db_session() as c:
                        c.execute("UPDATE inv_divergencia SET bling_msg=? WHERE id=?", (msg[:400], did))
                    return jsonify({"error": msg}), 502
                depois = NA._saldo_no_deposito(bs, pid, dep)
            except Exception as e:
                return jsonify({"error": f"{type(e).__name__}: {e}"[:300]}), 502
            msg = f"Saldo {antes:g} -> {depois:g} no depósito {dep}."
            ok = abs(depois - valor) < 1e-6
            with db_session() as c:
                c.execute("UPDATE inv_divergencia SET status=?, valor=?, decidido_por=?, decidido_em=?, bling_msg=? WHERE id=?",
                          ("substituida" if ok else "aberta", valor, eu, agora, msg if ok else msg + " Não ficou no valor pedido: confira no Bling.", did))
            if not ok:
                return jsonify({"error": msg + " Não ficou no valor pedido: confira no Bling."}), 502
            return jsonify({"ok": True, "status": "substituida", "msg": msg})
        return jsonify({"error": "Ação inválida."}), 400

    # ─────────────── inventário geral ───────────────
    @bp.get("/inventario/geral")
    @login_required
    def inventario_geral_ver():
        _tabelas()
        with db_session() as c:
            g = _geral_aberto(c) or (lambda r: dict(r) if r else None)(
                c.execute("SELECT * FROM inv_geral ORDER BY id DESC LIMIT 1").fetchone())
            out = _geral_resumo(c, g, _gestor()) if g else {"geral": None}
            if g and g["status"] == "procurando":
                out["faltam"] = _geral_faltam(c, int(g["id"]), _quem())
        out.update({"pode_comecar": _gestor(), "pode_encerrar": _admin(), "eu": _quem(), "gestor": _gestor()})
        return jsonify(out)

    @bp.post("/inventario/geral")
    @login_required
    def inventario_geral_comecar():
        if not _gestor():
            return jsonify({"error": "Só admin ou supervisor começa o inventário geral."}), 403
        _tabelas()
        with _lk_geral:
            with db_session() as c:
                if _geral_aberto(c):
                    return jsonify({"error": "Já tem um inventário geral aberto."}), 409
                est, ML = _estruturas_em_ordem()
                if not est:
                    return jsonify({"error": "O mapa não tem estruturas com posições: monte o mapa antes."}), 400
                agora, eu = int(time.time()), _quem()
                gid = _ins(c, "INSERT INTO inv_geral (status, mes, criado_por, criado_em, msg) VALUES ('contando', ?, ?, ?, ?)",
                           (_mes(), eu, agora, "Contando as estruturas."))
                por_pos, _ = _skus_no_mapa(c)
                n_t = n_p = 0
                for e, ps in est:
                    posicoes = [[int(e["id"]), int(p)] for p in ps]
                    n_prod = len({_key(it["sku"]) for pp, p in posicoes for it in por_pos.get((pp, p), [])})
                    ref = {"geral": gid, "mes": _mes(), "dia": _hoje(), "posicoes": posicoes, "estruturas": [e["id"]], "rodada": 1}
                    nome = e.get("curto") or e.get("nome") or str(e["id"])
                    tid = _ins(c, "INSERT INTO tarefas (tipo, titulo, descricao, para, criado_por, criado_em, prazo_em, status, ref) "
                                  "VALUES ('inventario', ?, ?, '', ?, ?, ?, 'aberta', ?)",
                               (f"Inventário geral: contar {nome}"[:200],
                                f"{len(posicoes)} posições, {n_prod} produtos no mapa. Conte tudo o que tem em cada posição; o sistema confere com o Bling.",
                                eu, agora, agora + 30 * 86400, json.dumps(ref)))
                    c.execute("INSERT INTO inv_geral_tarefa (geral_id, tarefa_id, tipo, sku_key) VALUES (?,?,?,NULL)", (gid, tid, "estrutura"))
                    n_t += 1
                    n_p += len(posicoes)
                # rotativo pausado: a contagem de hoje que ninguém começou sai da fila
                c.execute("UPDATE tarefas SET status='cancelada', obs_fim=? WHERE tipo='inventario' AND status='aberta' "
                          "AND id IN (SELECT tarefa_id FROM inv_dia WHERE dia=?)", ("Inventário geral começou.", _hoje()))
                g = c.execute("SELECT * FROM inv_geral WHERE id=?", (gid,)).fetchone()
                return jsonify({"ok": True, "geral": dict(g), "contagens": n_t, "posicoes": n_p})

    @bp.post("/inventario/geral/proxima")
    @login_required
    def inventario_geral_proxima():
        """Pega para quem tocou a próxima contagem do geral (recontagem primeiro). -> {tarefa_id} ou {tarefa_id: null}."""
        _tabelas()
        eu = _quem()
        with _lk_pega:
            with db_session() as c:
                g = _geral_aberto(c)
                if not g:
                    return jsonify({"tarefa_id": None, "msg": "Não tem inventário geral aberto."})
                r = c.execute("SELECT t.id FROM tarefas t JOIN inv_geral_tarefa x ON x.tarefa_id = t.id WHERE x.geral_id=? "
                              "AND t.status IN ('andamento','recontar') AND t.pego_por=? ORDER BY t.id LIMIT 1", (g["id"], eu)).fetchone()
                if r:
                    return jsonify({"tarefa_id": int(r["id"])})
                r = c.execute("SELECT t.id FROM tarefas t JOIN inv_geral_tarefa x ON x.tarefa_id = t.id WHERE x.geral_id=? "
                              "AND t.status='aberta' ORDER BY CASE WHEN x.tipo='recontar' THEN 0 ELSE 1 END, t.id LIMIT 1", (g["id"],)).fetchone()
                if not r:
                    return jsonify({"tarefa_id": None, "msg": "Todas as contagens já estão com alguém ou feitas."})
                c.execute("UPDATE tarefas SET status='andamento', pego_por=?, pego_em=? WHERE id=? AND status='aberta'",
                          (eu, int(time.time()), int(r["id"])))
                return jsonify({"tarefa_id": int(r["id"])})

    @bp.post("/inventario/geral/<int:gid>/item")
    @login_required
    def inventario_geral_item(gid):
        """Pop-up do que ninguém contou: {sku_key, acao: pegar | contar | nao_encontrei, qtd?, porta_palet?, posicao?}."""
        _tabelas()
        d = request.get_json(silent=True) or {}
        kk, acao, eu, agora = _key(d.get("sku_key") or d.get("sku")), str(d.get("acao") or ""), _quem(), int(time.time())
        with _lk_pega:
            with db_session() as c:
                g = c.execute("SELECT * FROM inv_geral WHERE id=?", (gid,)).fetchone()
                if not g or g["status"] != "procurando":
                    return jsonify({"error": "Esse inventário geral não está na fase de procurar."}), 409
                s = _gsku(c, gid, kk)
                if not s or s["status"] not in ("nao_contado", "contar_de_novo"):
                    return jsonify({"error": "Esse produto já foi resolvido.", "status": (s or {}).get("status")}), 409
                if s.get("msg") == "conferindo" and acao != "nao_encontrei":
                    return jsonify({"error": "Esse produto acabou de ser contado: o WMS está conferindo com o Bling."}), 409
                trava = s["trava_por"] if s.get("trava_em") and agora - int(s["trava_em"]) <= GERAL_TRAVA_S else None
                if trava and trava != eu and not _gestor():
                    return jsonify({"error": f"{trava} já está procurando esse produto."}), 409
                if acao == "pegar":
                    _gsku_gravar(c, gid, kk, trava_por=eu, trava_em=agora)
                    return jsonify({"ok": True})
                if acao == "contar":
                    try:
                        q = float(d.get("qtd"))
                    except (TypeError, ValueError):
                        return jsonify({"error": "Digite quantos achou."}), 400
                    if q < 0 or q > 100000:
                        return jsonify({"error": "Quantidade inválida."}), 400
                    pp = p = None
                    try:
                        if d.get("porta_palet") not in (None, "") and d.get("posicao") not in (None, ""):
                            pp, p = int(d["porta_palet"]), int(d["posicao"])
                    except (TypeError, ValueError):
                        pp = p = None
                    c.execute("INSERT INTO inv_geral_extra (geral_id, sku_key, sku, nome, qtd, porta_palet, posicao, por, em) VALUES (?,?,?,?,?,?,?,?,?)",
                              (gid, kk, s["sku"], s["nome"], q, pp, p, eu, agora))
                    if pp is not None and q > 0:       # disse onde achou: o mapa aprende
                        try:
                            if not c.execute("SELECT 1 FROM mapa_localizacao WHERE sku_key=? AND porta_palet=? AND posicao=?", (kk, pp, p)).fetchone():
                                c.execute("INSERT INTO mapa_localizacao (sku_key, sku, nome, porta_palet, posicao, atualizado_em) VALUES (?,?,?,?,?,?)",
                                          (kk, s["sku"], s["nome"], pp, p, agora))
                        except Exception:
                            pass
                    _gsku_gravar(c, gid, kk, por=eu, trava_por=eu, trava_em=agora, msg="conferindo")
                elif acao == "nao_encontrei":
                    pass
                else:
                    return jsonify({"error": "Ação inválida."}), 400
        if acao == "contar":
            threading.Thread(target=_avaliar, args=(gid, [kk], "extra"), kwargs={"forcar": True, "quem": eu}, daemon=True).start()
            return jsonify({"ok": True, "conferindo": True})
        ok, antes, msg = _zerar_inv(s["sku"], f"Por {eu}.")
        with db_session() as c:
            if not ok:
                _gsku_gravar(c, gid, kk, msg=msg)
                return jsonify({"error": f"Não consegui zerar no Bling: {msg}"}), 502
            _gsku_gravar(c, gid, kk, status="nao_encontrado", bling=antes, msg=msg, por=eu, trava_por=None, trava_em=None)
        _checar_fim(gid)
        return jsonify({"ok": True, "status": "nao_encontrado"})

    @bp.post("/inventario/geral/<int:gid>/devolver")
    @login_required
    def inventario_geral_devolver(gid):
        """Admin: o que foi zerado como Não encontrei apareceu -> devolve o saldo no Bling e volta para contar."""
        if not _admin():
            return jsonify({"error": "Só o admin devolve."}), 403
        _tabelas()
        kk = _key((request.get_json(silent=True) or {}).get("sku_key"))
        with db_session() as c:
            s = _gsku(c, gid, kk)
            g = c.execute("SELECT status FROM inv_geral WHERE id=?", (gid,)).fetchone()
        if not s or s["status"] != "nao_encontrado":
            return jsonify({"error": "Esse produto não está como Não encontrei."}), 409
        ok, msg = _devolver_inv(s["sku"], float(s["bling"] or 0), f"Por {_quem()}.")
        if not ok:
            return jsonify({"error": msg}), 502
        with db_session() as c:
            _gsku_gravar(c, gid, kk, status="nao_contado" if g and g["status"] == "procurando" else "devolvido", msg=msg, por=_quem())
        return jsonify({"ok": True, "msg": msg})

    @bp.post("/inventario/geral/<int:gid>/buscar")
    @login_required
    def inventario_geral_buscar(gid):
        if not _gestor():
            return jsonify({"error": "Só admin ou supervisor."}), 403
        with db_session() as c:
            g = c.execute("SELECT status FROM inv_geral WHERE id=?", (gid,)).fetchone()
            if not g or g["status"] != "buscando":
                return jsonify({"error": "Agora não é a hora de procurar no Bling."}), 409
            c.execute("UPDATE inv_geral SET msg=? WHERE id=?", ("Procurando no Bling o que ninguém contou…", gid))
        threading.Thread(target=_fase2, args=(gid,), daemon=True).start()
        return jsonify({"ok": True})

    @bp.post("/inventario/geral/<int:gid>/encerrar")
    @login_required
    def inventario_geral_encerrar(gid):
        """Admin: encerra agora (o que ficou aberto é cancelado; divergências continuam para decidir)."""
        if not _admin():
            return jsonify({"error": "Só o admin encerra o inventário geral."}), 403
        cancelar = bool((request.get_json(silent=True) or {}).get("cancelar"))
        with _lk_geral:
            with db_session() as c:
                g = c.execute("SELECT * FROM inv_geral WHERE id=?", (gid,)).fetchone()
                if not g or g["status"] not in GERAL_ABERTO:
                    return jsonify({"error": "Esse inventário geral já foi encerrado."}), 409
                agora = int(time.time())
                c.execute("UPDATE tarefas SET status='cancelada', feito_por=?, feito_em=?, obs_fim=? WHERE status IN ('aberta','andamento','recontar') "
                          "AND id IN (SELECT tarefa_id FROM inv_geral_tarefa WHERE geral_id=?)", (_quem(), agora, "Inventário geral encerrado.", gid))
                c.execute("UPDATE inv_geral SET status=?, encerrado_por=?, encerrado_em=?, msg=? WHERE id=?",
                          ("cancelado" if cancelar else "encerrado", _quem(), agora,
                           "Cancelado pelo admin." if cancelar else "Encerrado pelo admin.", gid))
        return jsonify({"ok": True})

    # ─────────────── pedido parado na Separação ───────────────
    @bp.get("/separacao/paradas")
    @login_required
    def sep_paradas_listar():
        """Gestor: cobranças de hoje/ontem com as respostas; operador: só as dele abertas."""
        _tabelas()
        try:
            _detectar_paradas()
        except Exception:
            pass
        eu = _quem()
        with db_session() as c:
            abertas = _paradas_para(c, eu, gestor=_gestor())
            hist = []
            if _gestor():
                hist = [dict(r) for r in c.execute("SELECT * FROM sep_parada WHERE respondido_em >= ? ORDER BY respondido_em DESC LIMIT 200",
                                                   (int(time.time()) - 48 * 3600,)).fetchall()]
        return jsonify({"abertas": abertas, "respondidas": hist, "gestor": _gestor(), "eu": eu, "horas": PARADA_HORAS(), "corte": PARADA_CORTE(),
                        "ligado": PARADA_LIGADA(), "agora": int(time.time())})

    @bp.get("/separacao/paradas/<path:ml_id>/itens")
    @login_required
    def sep_paradas_itens(ml_id):
        """Itens do pedido (para 'Não achei o produto' escolher qual)."""
        with db_session() as c:
            rows = c.execute("""SELECT pr.sku, pr.nome, pp.quantidade, pp.quantidade_bipada FROM pedidos p
                                JOIN pedido_produtos pp ON pp.pedido_id = p.id JOIN produtos pr ON pr.id = pp.produto_id
                                WHERE p.ml_id=? ORDER BY pp.id""", (ml_id,)).fetchall()
        return jsonify({"itens": [{"sku": r["sku"], "nome": r["nome"], "qtd": int(r["quantidade"] or 0), "bipados": int(r["quantidade_bipada"] or 0)} for r in rows]})

    @bp.post("/separacao/paradas/<int:pid>/responder")
    @login_required
    def sep_paradas_responder(pid):
        _tabelas()
        d = request.get_json(silent=True) or {}
        resp = str(d.get("resposta") or "")
        texto = str(d.get("texto") or "").strip()[:500]
        sku = str(d.get("sku") or "").strip()[:64]
        eu, agora = _quem(), int(time.time())
        if resp not in ("nao_achei", "falta_produto", "cancelado", "termino_agora", "outro"):
            return jsonify({"error": "Escolha uma resposta."}), 400
        if resp == "outro" and len(texto) < 10:
            return jsonify({"error": "Escreva o que aconteceu (pelo menos 10 letras)."}), 400
        if resp == "nao_achei" and not sku:
            return jsonify({"error": "Diga qual produto você não achou."}), 400
        with db_session() as c:
            c.execute("UPDATE sep_parada SET id = id WHERE id = ?", (pid,))
            p = c.execute("SELECT * FROM sep_parada WHERE id=?", (pid,)).fetchone()
            if not p:
                return jsonify({"error": "Cobrança não encontrada."}), 404
            p = dict(p)
            if p["resposta"] and not (p["resposta"] == "termino_agora" and (p["prazo_em"] or 0) <= agora):
                return jsonify({"error": f"Já respondida por {p['respondido_por']}."}), 409
            if p["cobrado_de"] and p["cobrado_de"] != eu and not _gestor():
                return jsonify({"error": f"Essa cobrança é de {p['cobrado_de']}."}), 403
            ped = c.execute("SELECT * FROM pedidos WHERE ml_id=?", (p["ml_id"],)).fetchone()
            ped = dict(ped) if ped else None
            feito = None

            def _zerar(motivo):
                """Biel 19/11 16:15: "depois da resposta tem que zerar" -> o pedido sai da Separação (arquivado, volta em Arquivados)."""
                if ped and ped["status"] in ("PENDENTE", "PARCIAL"):
                    c.execute("UPDATE pedidos SET status='ARQUIVADO' WHERE id=? AND status=?", (ped["id"], ped["status"]))
                    try:
                        c.execute("INSERT INTO pedidos_arquivo (ml_id, em, status_antes, motivo, por, origem, cliente) VALUES (?,?,?,?,?,?,?)",
                                  (p["ml_id"], agora * 1000, ped["status"], (motivo + (". " + texto if texto else ""))[:200], eu,
                                   str(ped.get("logistic_type") or "ML"), str(ped.get("cliente_nome") or "")[:120]))
                    except Exception:
                        pass
                    return "pedido saiu da Separação (arquivado; volta em Arquivados)"
                return "pedido já não estava na Separação"

            if resp == "cancelado":
                feito = _zerar("Separação parada: cliente/ML cancelou")
            elif resp == "falta_produto":
                feito = _zerar("Separação parada: falta produto, comprando")
            elif resp == "outro":
                feito = _zerar("Separação parada: " + texto)
            elif resp == "nao_achei":
                try:
                    import naoachado_routes as NA
                    NA.garantir_tabelas()
                    nome = c.execute("SELECT nome FROM produtos WHERE UPPER(sku)=UPPER(?)", (sku,)).fetchone()
                    nome = nome["nome"] if nome else ""
                    ex = c.execute("SELECT id FROM nao_achado WHERE sku_key=? AND status='aberto' AND COALESCE(ml_id,'')=? AND reportado_em > ?",
                                   (sku.lower(), p["ml_id"], agora - 6 * 3600)).fetchone()
                    if ex:
                        nid = int(ex["id"])
                        feito = f"Não achado já aberto (#{nid})"
                    else:
                        prazo = agora + int(NA.horas_prazo() * 3600)
                        c.execute("INSERT INTO nao_achado (sku, sku_key, nome, qtd, ml_id, estoque_bling, locais, obs, reportado_por, reportado_em, prazo_em) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                  (sku, sku.lower(), nome[:200], 1.0, p["ml_id"], None, "", ("Separação parada: " + texto)[:300], eu, agora, prazo))
                        nid = int(c.execute("SELECT id FROM nao_achado WHERE sku_key=? AND reportado_em=? ORDER BY id DESC LIMIT 1", (sku.lower(), agora)).fetchone()["id"])
                        feito = f"Não achado #{nid} aberto" + (" (zera o Bling e avisa o compras)" if NA._zerar_ligado() and NA._zerar_no_relato() else "")
                        if NA._zerar_no_relato():
                            threading.Thread(target=NA._zerar_agora, args=(nid,), daemon=True).start()
                except Exception as e:
                    feito = f"não consegui abrir o Não achado: {type(e).__name__}: {str(e)[:120]}"
                feito = (feito or "") + " · " + _zerar("Separação parada: não achou " + sku)
            elif resp == "termino_agora":
                feito = "30 min para terminar; depois cobra de novo"
            c.execute("UPDATE sep_parada SET respondido_por=?, respondido_em=?, resposta=?, texto=?, sku=?, prazo_em=?, feito=? WHERE id=?",
                      (eu, agora, resp, texto or None, sku or None, agora + 1800 if resp == "termino_agora" else None, feito, pid))
        return jsonify({"ok": True, "feito": feito})
