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
                             "so_skus": bool(ref.get("so_skus"))}
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
        return jsonify({"inventario": inv, "novas": novas, "divergencias": div, "intervalo_min": int(_env("INV_AVISO_MIN", "15") or 15),
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
            c.execute("UPDATE tarefas SET status='conferindo' WHERE id=?", (tid,))
        _conferindo.add(tid)
        threading.Thread(target=_conferir, args=(tid,), daemon=True).start()
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
