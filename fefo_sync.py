#!/usr/bin/env python3
"""
Sincroniza lotes + saldo por lote do Bling para a cópia local do WMS (tabelas fefo_*).

Rode DENTRO da pasta do backend, com o venv do WMS:
    python fefo_sync.py --pendentes            # SKUs de pedidos ainda não concluídos
    python fefo_sync.py --skus MP001,V000023   # SKUs específicos
    python fefo_sync.py --arquivo skus.txt     # um SKU por linha
    python fefo_sync.py --pendentes --max 80   # limite por rodada (padrão 150)

Cuidados com 429 (leitura apenas — este script NUNCA escreve no Bling):
  * nunca renova token: lê o token que o cron já mantém; vencido => encerra
  * >= 0.6 s entre chamadas; timeout curto; ~3 chamadas por SKU na 1ª vez, 2 depois
  * ao primeiro 429/401/403 PARA (mostra Retry-After) e retoma na próxima rodada
  * SKU sincronizado há pouco é pulado (--horas, padrão 6; sem lote: 24 h)
  * processa primeiro quem está há mais tempo sem sincronizar
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

try:                       # mesmo ajuste de JWT (enable-jwt) que o WMS usa; o cabeçalho abaixo cobre se faltar
    import bling_jwt  # noqa: F401
except Exception:
    pass

from db import db_session
from fefo_routes import garantir_tabelas, lote_key, sku_key
from services.bling_service import BlingService

BASE = "https://api.bling.com.br/Api/v3"
PAUSA = 0.6
TIMEOUT = (3, 10)


class Parar(Exception):
    pass


class Bling:
    def __init__(self):
        self.bs = BlingService()
        self.ultima = 0.0
        self.dep = None

    def get(self, path, params=None):
        t = self.bs._load_tokens() or {}
        try:
            exp = float(t.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0.0
        if not t.get("access_token") or time.time() > exp - 120:
            raise Parar("token vencido — espere o cron renovar (renovar_bling_tokens.sh) e rode de novo")
        espera = self.ultima + PAUSA - time.time()
        if espera > 0:
            time.sleep(espera)
        self.ultima = time.time()
        try:
            r = requests.get(BASE + path, params=params, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {t['access_token']}", "Accept": "application/json",
                                     "enable-jwt": "1"})
        except requests.RequestException as e:
            raise Parar(f"rede: {e}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            raise Parar(f"429 do Bling (Retry-After {r.headers.get('Retry-After', '?')}s) — parei de propósito")
        if r.status_code in (401, 403):
            raise Parar(f"{r.status_code} do Bling — token/permissão; não insisto")
        if r.status_code >= 500:
            raise Parar(f"{r.status_code} do Bling")
        return None

    def deposito(self):
        if self.dep:
            return self.dep
        env = os.environ.get("BLING_DEPOSITO_ID", "").strip()
        if env:
            self.dep = int(env)
            return self.dep
        r = self.get("/depositos", {"limite": 100}) or {}
        deps = r.get("data") or []
        pad = next((d for d in deps if d.get("padrao")), deps[0] if deps else None)
        if not pad:
            raise Parar("não achei depósito (defina BLING_DEPOSITO_ID)")
        self.dep = int(pad["id"])
        print(f"depósito usado: {self.dep} ({pad.get('descricao')})  [fixe com BLING_DEPOSITO_ID]")
        return self.dep


def skus_pendentes(conn):
    rows = conn.execute("""
        SELECT DISTINCT pr.sku AS sku
        FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        JOIN pedidos p ON p.id = pp.pedido_id
        WHERE p.status <> 'CONCLUIDO'
    """).fetchall()
    return [r["sku"] for r in rows if r["sku"]]


def ordenar_e_filtrar(conn, skus, horas, forcar):
    agora = int(time.time())
    out = []
    for s in dict.fromkeys(x.strip() for x in skus if x and x.strip()):
        r = conn.execute("SELECT id_bling, n_lotes, sync_em FROM fefo_sku WHERE sku_key=?", (sku_key(s),)).fetchone()
        if r and not forcar:
            idade = agora - int(r["sync_em"])
            if idade < (horas * 3600 if r["n_lotes"] else max(horas, 24) * 3600):
                continue
        out.append((int(r["sync_em"]) if r else 0, s, r["id_bling"] if r else None))
    out.sort()
    return [(s, i) for _, s, i in out]


def sincronizar_sku(b: Bling, sku: str, id_bling):
    if not id_bling:
        r = b.get("/produtos", {"codigo": sku, "limite": 5}) or {}
        data = r.get("data") or []
        prod = next((p for p in data if str(p.get("codigo", "")).lower() == sku.lower()), None)
        id_bling = prod.get("id") if prod else None
        if not id_bling:
            return None, []                       # produto não existe no Bling
    r = b.get("/produtos/lotes", {"idsProdutos[]": id_bling}) or {}
    lotes = r.get("data") or []
    if not lotes:
        return id_bling, []
    ids = [l["idLote"] for l in lotes if l.get("idLote") is not None]
    r = b.get(f"/produtos/{id_bling}/lotes/depositos/{b.deposito()}/saldo", {"idsLotes[]": ids}) or {}
    saldos = {it["idLote"]: float(it.get("saldoAtual") or 0)
              for it in (r.get("data") or []) if it.get("idLote") is not None}
    return id_bling, [{
        "id_lote": l["idLote"], "lote": l.get("codigoLote") or "", "validade": (l.get("dataValidade") or "")[:10] or None,
        "saldo": saldos.get(l["idLote"], 0.0)} for l in lotes if l.get("idLote") is not None and l.get("codigoLote")]


def gravar(conn, sku, id_bling, lotes):
    agora = int(time.time())
    key = sku_key(sku)
    conn.execute("DELETE FROM fefo_lote WHERE sku_key=?", (key,))
    for l in lotes:
        conn.execute(
            "INSERT INTO fefo_lote (sku_key, id_lote, lote, lote_key, validade, saldo, sync_em) VALUES (?,?,?,?,?,?,?)",
            (key, l["id_lote"], l["lote"], lote_key(l["lote"]), l["validade"], l["saldo"], agora))
    conn.execute("""
        INSERT INTO fefo_sku (sku_key, sku, id_bling, n_lotes, sync_em) VALUES (?,?,?,?,?)
        ON CONFLICT (sku_key) DO UPDATE SET sku=EXCLUDED.sku, id_bling=EXCLUDED.id_bling,
            n_lotes=EXCLUDED.n_lotes, sync_em=EXCLUDED.sync_em
    """, (key, sku, id_bling, len(lotes), agora))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus")
    ap.add_argument("--arquivo")
    ap.add_argument("--pendentes", action="store_true")
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--horas", type=float, default=6)
    ap.add_argument("--forcar", action="store_true")
    a = ap.parse_args()

    garantir_tabelas()
    skus = []
    if a.skus:
        skus += a.skus.split(",")
    if a.arquivo:
        skus += open(a.arquivo, encoding="utf-8").read().split()
    if a.pendentes:
        try:
            with db_session() as conn:
                skus += skus_pendentes(conn)
        except Exception as e:
            print(f"não consegui listar SKUs pendentes ({e}). Use --skus ou --arquivo.")
            return 2
    if not skus:
        print("nenhum SKU informado (use --pendentes, --skus ou --arquivo)")
        return 2

    with db_session() as conn:
        fila = ordenar_e_filtrar(conn, skus, a.horas, a.forcar)[: a.max]
    print(f"{len(fila)} SKU(s) para sincronizar")
    b = Bling()
    ok = com_lote = 0
    try:
        for sku, id_bling in fila:
            idb, lotes = sincronizar_sku(b, sku, id_bling)
            with db_session() as conn:
                gravar(conn, sku, idb, lotes)
            ok += 1
            com_lote += 1 if lotes else 0
            print(f"  {sku}: {len(lotes)} lote(s)" + (f", saldo {sum(l['saldo'] for l in lotes):g}" if lotes else ""))
    except Parar as e:
        print(f"PAREI: {e}")
        print(f"feitos nesta rodada: {ok} ({com_lote} com lote)")
        return 1
    print(f"ok: {ok} SKU(s), {com_lote} com lote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
