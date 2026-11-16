"""Mapa montável (Biel 16/11: "para esse sistema rodar em qualquer operação ... o cara montar o próprio mapa dele").

O desenho do galpão deixa de ser fixo no código e vira um LAYOUT guardado no banco:
  estruturas  porta-pallet / cesta / refrigerador, cada uma com id (= mapa_localizacao.porta_palet), lugar na planta
              (x, y, w, h em quadradinhos), grade de posições (numeros: linhas x colunas com o número de cada posição),
              nome, sigla, prefixo da placa (LOC-<placa>-<letra><n>), letra/palavra da posição, lado de acesso, cor
  elementos   corredor, entrada, área (só desenho; a rota passa pelos corredores)
  ordem       ids na ordem em que o separador anda (arrastada pelo admin); "ultimo": sempre no fim (refrigerador)
O layout padrão é EXATAMENTE o mapa de hoje da LHVMed (mesmos códigos, placas e ordem da rota): nada muda para quem separa.

Rotas (registradas no blueprint do mapa, /api):
  GET  /mapa/layout                 layout em uso (+ versão)
  POST /mapa/layout                 salva (só admin); não deixa sumir posição com SKU guardado (a não ser com forcar)
  GET  /mapa/layout/historico       versões salvas
  POST /mapa/layout/voltar          {id}: volta para uma versão (só admin)"""
from __future__ import annotations

import copy
import json
import threading
import time

from flask import jsonify, request
from flask_login import current_user, login_required

try:
    from db import db_session
except ImportError:                   # pragma: no cover
    from db.database import db_session

_lk = threading.Lock()
_cache = {"layout": None, "versao": None, "em": 0}
_ok_tab = False


# ───────────────────────── layout padrão = mapa atual da LHVMed ─────────────────────────
def _pp(id_, nome, curto, placa, fala, x, y, numeros, acesso, cor, w=18, h=12):
    return {"id": id_, "tipo": "porta_pallet", "nome": nome, "curto": curto, "placa": placa, "fala": fala, "x": x, "y": y, "w": w, "h": h,
            "numeros": numeros, "letra": "P", "palavra": "posição", "acesso": acesso, "cor": cor, "ultimo": False}


def layout_padrao():
    cores = ["#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4", "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#84cc16"]
    cima, baixo = [[6, 5], [4, 3], [2, 1]], [[1, 2], [3, 4], [5, 6]]       # P1/P2 sempre junto do corredor
    est = [{"id": 51, "tipo": "refrigerador", "nome": "Refrigerador", "curto": "Refrigerador", "placa": "REF1", "fala": "refrigerador",
            "x": 2, "y": 2, "w": 9, "h": 9, "numeros": [[1]], "letra": "P", "palavra": "posição", "acesso": "baixo", "cor": "#64748b", "ultimo": True}]
    for i, n in enumerate((1, 2)):
        est.append(_pp(30 + n, f"Porta Palet Vet {n}", f"Vet {n}", f"PPV{n}", f"porta palete veterinário {n}", 40 + i * 16, 1, cima, "baixo",
                       "#0ea5e9", w=12, h=10))
    for i in range(11):
        n = i + 1
        est.append({"id": 100 + n, "tipo": "cesta", "nome": f"Cesta {n}", "curto": f"Cesta {n}", "placa": f"C{n}", "fala": f"cesta {n}",
                    "x": 12 + i * 7, "y": 14, "w": 6, "h": 14, "numeros": [[f] for f in range(1, 8)], "letra": "F", "palavra": "fileira",
                    "acesso": "baixo", "cor": "#a855f7", "ultimo": False})
    for i in range(5):
        top, bot = 6 + i, 1 + i
        est.append(_pp(top, f"Porta Palet {top}", f"PP{top}", f"PP{top}", f"porta palete {top}", 12 + i * 21, 31, cima, "baixo", cores[top - 1]))
        est.append(_pp(bot, f"Porta Palet {bot}", f"PP{bot}", f"PP{bot}", f"porta palete {bot}", 12 + i * 21, 53, baixo, "cima", cores[bot - 1]))
    ordem = list(range(101, 112)) + [31, 32] + [x for i in range(1, 6) for x in (i, i + 5)] + [51]
    return {"versao_formato": 1, "grade": {"w": 118, "h": 66}, "estruturas": est,
            "elementos": [{"tipo": "corredor", "x": 0, "y": 44, "w": 118, "h": 8, "nome": "Corredor"},
                          {"tipo": "corredor", "x": 0, "y": 11, "w": 11, "h": 33, "nome": ""},
                          {"tipo": "corredor", "x": 11, "y": 11, "w": 58, "h": 3, "nome": ""},     # fundo: frente dos vet
                          {"tipo": "corredor", "x": 11, "y": 28, "w": 78, "h": 3, "nome": ""},     # frente das cestas
                          {"tipo": "entrada", "x": 0, "y": 46, "w": 4, "h": 4, "nome": "Início"}],
            "ordem": ordem}


# ───────────────────────── banco ─────────────────────────
def _tab():
    global _ok_tab
    if _ok_tab:
        return
    import os
    pk = "BIGSERIAL PRIMARY KEY" if os.environ.get("DATABASE_URL") else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with db_session() as c:
        c.execute(f"CREATE TABLE IF NOT EXISTS mapa_layout (id {pk}, dados TEXT NOT NULL, criado_por TEXT, criado_em BIGINT, obs TEXT)")
    _ok_tab = True


def layout():
    """Layout em uso (cache de 30 s). Sem nenhum salvo: o padrão (mapa da LHVMed)."""
    with _lk:
        if _cache["layout"] is not None and time.time() - _cache["em"] < 30:
            return _cache["layout"], _cache["versao"]
    try:
        _tab()
        with db_session() as c:
            r = c.execute("SELECT id, dados FROM mapa_layout ORDER BY id DESC LIMIT 1").fetchone()
        lay, ver = (json.loads(r["dados"]), int(r["id"])) if r else (layout_padrao(), 0)
    except Exception:
        lay, ver = layout_padrao(), 0
    with _lk:
        _cache.update(layout=lay, versao=ver, em=time.time())
    return lay, ver


def _por_id(lay=None):
    lay = lay or layout()[0]
    return {int(e["id"]): e for e in lay.get("estruturas") or []}


def posicoes(e):
    return [int(n) for linha in e.get("numeros") or [] for n in linha if n not in (None, "", 0)]


# ───────────────────────── usados pelo resto do WMS ─────────────────────────
def valido(pp, p):
    e = _por_id().get(int(pp))
    return bool(e) and int(p) in posicoes(e)


def rank(pp, p):
    """Ordem da rota: posição da estrutura na lista arrastada x 100 + número da posição; "ultimo" vai para o fim."""
    lay = layout()[0]
    e = _por_id(lay).get(int(pp))
    if not e:
        return 90000                    # estrutura que saiu do mapa: depois de tudo
    ordem = [int(x) for x in lay.get("ordem") or []]
    i = ordem.index(int(pp)) if int(pp) in ordem else len(ordem)
    return (50000 if e.get("ultimo") else 0) + i * 100 + int(p or 0)     # até 99 estruturas x 99 posições < 10000


def zona(pp):
    e = _por_id().get(int(pp))
    t = (e or {}).get("tipo")
    return {"cesta": "cesta", "refrigerador": "refri"}.get(t, "vet" if e and str(e.get("placa", "")).upper().startswith("PPV") else "pp")


def rotulo(pp, p):
    e = _por_id().get(int(pp))
    if not e:
        return f"PP{pp} · P{p}"
    return e["curto"] if len(posicoes(e)) <= 1 else f"{e['curto']} · {e.get('letra') or 'P'}{p}"


def endereco(pp, p):
    """(código curto, texto longo) — o mesmo que o recebimento mostra em "onde guardar"."""
    e = _por_id().get(int(pp))
    if not e:
        return f"PP{pp}-P{p}", f"Porta Palet {pp} · Posição P{p}"
    cod = f"{e['placa']}-{e.get('letra') or 'P'}{p}"
    if len(posicoes(e)) <= 1:
        return cod, e["nome"]
    num = f"{e.get('letra') or 'P'}{p}" if (e.get("letra") or "P").upper() == "P" else str(p)   # "Posição P3", "Fileira 2" (como era)
    return cod, f"{e['nome']} · {str(e.get('palavra') or 'posição').capitalize()} {num}"


# ───────────────────────── validação ao salvar ─────────────────────────
TIPOS = ("porta_pallet", "cesta", "refrigerador")


def _validar(lay):
    erros = []
    est = lay.get("estruturas") or []
    if not est:
        erros.append("O mapa precisa de pelo menos uma estrutura.")
    ids, placas = set(), set()
    for e in est:
        try:
            i = int(e.get("id"))
        except (TypeError, ValueError):
            erros.append(f"Estrutura sem número: {e.get('nome')}"); continue
        if i <= 0 or i in ids:
            erros.append(f"Número repetido ou inválido: {i} ({e.get('nome')})")
        ids.add(i)
        if e.get("tipo") not in TIPOS:
            erros.append(f"{e.get('nome')}: tipo inválido")
        if not str(e.get("nome") or "").strip() or not str(e.get("curto") or "").strip():
            erros.append(f"Estrutura {i}: falta nome ou sigla")
        pl = str(e.get("placa") or "").strip().upper()
        if not pl or not pl.replace("_", "").isalnum():
            erros.append(f"{e.get('nome')}: código da placa só com letras e números")
        elif pl in placas:
            erros.append(f"Código de placa repetido: {pl}")
        placas.add(pl)
        ps = posicoes(e)
        if not ps:
            erros.append(f"{e.get('nome')}: sem nenhuma posição")
        if len(ps) != len(set(ps)):
            erros.append(f"{e.get('nome')}: número de posição repetido")
        if any(p > 99 for p in ps):
            erros.append(f"{e.get('nome')}: posição vai até 99")
    lay["ordem"] = [int(x) for x in lay.get("ordem") or [] if int(x) in ids] + sorted(ids - {int(x) for x in lay.get("ordem") or []})
    return erros


def _quem():
    try:
        return str(getattr(current_user, "username", None) or getattr(current_user, "id", "") or "")[:80]
    except Exception:
        return ""


def _admin():
    try:
        return str(getattr(current_user, "role", "") or "").lower() == "admin"
    except Exception:
        return False


def registrar(bp):
    @bp.get("/mapa/layout")
    @login_required
    def mapa_layout_ver():
        lay, ver = layout()
        return jsonify({"layout": lay, "versao": ver, "padrao": ver == 0, "pode_editar": _admin()})

    @bp.post("/mapa/layout")
    @login_required
    def mapa_layout_salvar():
        if not _admin():
            return jsonify({"error": "Só admin muda o mapa."}), 403
        d = request.get_json(silent=True) or {}
        lay = copy.deepcopy(d.get("layout") or {})
        erros = _validar(lay)
        if erros:
            return jsonify({"error": erros[0], "erros": erros}), 400
        # posição com SKU guardado não pode sumir sem querer
        novas = {(int(e["id"]), p) for e in lay["estruturas"] for p in posicoes(e)}
        _tab()
        with db_session() as c:
            usadas = c.execute("SELECT porta_palet, posicao, COUNT(*) AS n, MIN(sku) AS ex FROM mapa_localizacao GROUP BY porta_palet, posicao").fetchall()
        perdidas = [(int(u["porta_palet"]), int(u["posicao"]), int(u["n"]), u["ex"]) for u in usadas if (int(u["porta_palet"]), int(u["posicao"])) not in novas]
        if perdidas and not d.get("forcar"):
            pp, p, n, ex = perdidas[0]
            return jsonify({"error": f"{len(perdidas)} posição(ões) com produto guardado sumiriam (ex.: {rotulo(pp, p)} com {ex}). "
                                     "Tire os produtos dessas posições no mapa antes, ou salve mesmo assim (eles ficam sem lugar).",
                            "perdidas": [{"pp": a, "p": b, "n": n2, "ex": e} for a, b, n2, e in perdidas[:50]], "tipo": "perde_posicao"}), 409
        with db_session() as c:
            c.execute("INSERT INTO mapa_layout (dados, criado_por, criado_em, obs) VALUES (?,?,?,?)",
                      (json.dumps(lay, ensure_ascii=False), _quem(), int(time.time()), str(d.get("obs") or "")[:200] or None))
        with _lk:
            _cache.update(layout=None, em=0)
        lay2, ver = layout()
        return jsonify({"success": True, "layout": lay2, "versao": ver, "sem_lugar": len(perdidas)})

    @bp.get("/mapa/layout/historico")
    @login_required
    def mapa_layout_historico():
        _tab()
        with db_session() as c:
            rs = [dict(r) for r in c.execute("SELECT id, criado_por, criado_em, obs FROM mapa_layout ORDER BY id DESC LIMIT 30").fetchall()]
        return jsonify({"versoes": rs})

    @bp.post("/mapa/layout/voltar")
    @login_required
    def mapa_layout_voltar():
        if not _admin():
            return jsonify({"error": "Só admin muda o mapa."}), 403
        vid = int((request.get_json(silent=True) or {}).get("id") or 0)
        _tab()
        with db_session() as c:
            if vid == 0:
                dados = json.dumps(layout_padrao(), ensure_ascii=False)
            else:
                r = c.execute("SELECT dados FROM mapa_layout WHERE id=?", (vid,)).fetchone()
                if not r:
                    return jsonify({"error": "Versão não encontrada."}), 404
                dados = r["dados"]
            c.execute("INSERT INTO mapa_layout (dados, criado_por, criado_em, obs) VALUES (?,?,?,?)",
                      (dados, _quem(), int(time.time()), f"voltou para a versão {vid or 'padrão'}"))
        with _lk:
            _cache.update(layout=None, em=0)
        lay, ver = layout()
        return jsonify({"success": True, "layout": lay, "versao": ver})
