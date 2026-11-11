"""Comercial: quando o pedido é conferido na Expedição, o pedido de venda no Bling vai para "Pronto para faturar".
Troca SÓ a situação (PATCH /pedidos/vendas/{id}/situacoes/{idSituacao}); não regrava o pedido.
Nome da situação: COMERCIAL_SITUACAO_FATURAR no .env (padrão "Pronto para faturar"; aceita o id também).

CLI (rodar na pasta do backend):
  venv/bin/python -u comercial_faturar.py --listar            situações de venda do Bling (confere o nome)
  venv/bin/python -u comercial_faturar.py --aplicar [--so-ver] passa os do Comercial já conferidos que ainda estão "Em andamento"
"""
import os
import re
import sys
import unicodedata

_SID = None


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s.replace(" pra ", " para ")).strip()


def _bling():
    from services.bling_service import BlingService
    return BlingService()


def situacoes(b=None):
    b = b or _bling()
    mid = b.obter_modulo_id_pedidos_vendas()
    return [(int(s.get("id")), str(s.get("nome") or s.get("descricao") or "")) for s in b.listar_situacoes_do_modulo(mid) if s.get("id")]


def situacao_faturar_id(b=None):
    global _SID
    if _SID:
        return _SID
    alvo = (os.environ.get("COMERCIAL_SITUACAO_FATURAR") or "Pronto para faturar").strip()
    if alvo.isdigit():
        _SID = int(alvo)
        return _SID
    for sid, nome in situacoes(b):
        if _norm(nome) == _norm(alvo):
            _SID = sid
            return sid
    raise RuntimeError(f'não existe a situação "{alvo}" nas vendas do Bling (veja com --listar)')


def _pv_id(ml_id):
    from db import db_session
    with db_session() as c:
        r = c.execute("SELECT * FROM pedidos WHERE ml_id=?", (str(ml_id),)).fetchone()
    r = dict(r) if r else {}
    pv = r.get("bling_pedido_venda_id")
    return int(pv) if pv else None


def marcar_pronto_para_faturar(ml_id, pv_id=None):
    """-> {"ok": bool, "pv": id, "situacao": id, "erro": str}. Nunca levanta exceção (não pode travar o bip)."""
    try:
        pv_id = pv_id or _pv_id(ml_id)
        if not pv_id:
            return {"ok": False, "erro": "pedido sem id do Bling"}
        b = _bling()
        sid = situacao_faturar_id(b)
        r = b._request("PATCH", f"/pedidos/vendas/{int(pv_id)}/situacoes/{sid}")
        ok = r is not None and r.status_code in (200, 201, 204)
        print(f"[COMERCIAL] PV {pv_id} -> situação {sid} (pronto para faturar): HTTP {getattr(r, 'status_code', '-')}", flush=True)
        return {"ok": ok, "pv": pv_id, "situacao": sid, "erro": None if ok else (r.text[:300] if r is not None else "sem resposta")}
    except Exception as e:
        print(f"[COMERCIAL] pronto para faturar falhou ({ml_id}): {e}", flush=True)
        return {"ok": False, "erro": str(e)[:300]}


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv; load_dotenv(".env")
    except Exception:
        pass
    a = sys.argv[1:]
    if "--listar" in a:
        for sid, nome in situacoes():
            print(f"   {sid:>8}  {nome}")
        sys.exit(0)
    if "--aplicar" in a:
        from db import db_session
        b = _bling()
        sid = situacao_faturar_id(b)
        print(f'situação "Pronto para faturar" = {sid}')
        with db_session() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT pe.ml_id, pe.cliente_nome, pe.status, pe.bling_pedido_venda_id, "
                "SUM(pp.quantidade) AS q, SUM(pp.quantidade_bipada) AS bip FROM pedidos pe "
                "JOIN pedido_produtos pp ON pp.pedido_id=pe.id WHERE pe.origem='COMERCIAL' "
                "AND pe.status IN ('SEPARADO','CONCLUIDO') GROUP BY pe.ml_id, pe.cliente_nome, pe.status, pe.bling_pedido_venda_id").fetchall()]
        for r in rows:
            if not r["bling_pedido_venda_id"] or int(r["bip"] or 0) < int(r["q"] or 0):
                continue
            det = b._get_json(f"/pedidos/vendas/{int(r['bling_pedido_venda_id'])}").get("data") or {}
            atual = (det.get("situacao") or {}).get("id")
            if atual == sid:
                print(f"ok      PV {det.get('numero')} {r['cliente_nome']} já está pronto para faturar")
                continue
            if atual != 15:
                print(f"PULEI   PV {det.get('numero')} {r['cliente_nome']}: situação {atual} (não está Em andamento)")
                continue
            if "--so-ver" in a:
                print(f"PASSARIA PV {det.get('numero')} {r['cliente_nome']} -> pronto para faturar")
                continue
            res = marcar_pronto_para_faturar(r["ml_id"], r["bling_pedido_venda_id"])
            print(("FEITO   " if res["ok"] else "ERRO    ") + f"PV {det.get('numero')} {r['cliente_nome']} {res.get('erro') or ''}")
