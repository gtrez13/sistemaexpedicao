#!/usr/bin/env bash
# Instalador do sincronizador automatico de estoque (rode DENTRO da pasta do backend da LHVMed).
#   bash instalar_estoque_sync.sh          -> instala o script e faz um teste SEM gravar (--dry)
#   bash instalar_estoque_sync.sh --cron   -> liga a execucao automatica a cada 5 minutos
set -e
[ -f app.py ] || { echo "ERRO: rode dentro da pasta do backend (onde esta o app.py)"; exit 1; }
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed (RussianL). Pasta atual: $PWD"; exit 1;; esac
DEP="${BLING_DEPOSITO_ID:-10488672491}"

if [ "${1:-}" != "--cron" ]; then
cat > estoque_sync.py <<'ESEOF'
#!/usr/bin/env python3
"""
Mantém o estoque do Bling (coluna produtos.estoque_bling) atualizado SOZINHO para todo SKU de pedido
ainda não concluído. Roda por cron, fora do servidor web (não deixa o sistema lento).

    python estoque_sync.py                 # SKUs de pedidos não concluídos (uso normal, cron)
    python estoque_sync.py --skus A,B      # SKUs específicos
    python estoque_sync.py --dry           # só mostra o que mudaria, não grava

Estoque = saldo FÍSICO do depósito da LHVMed (BLING_DEPOSITO_ID), igual ao resto do WMS (ignora FULL negativo).

Cuidados com o Bling (só LEITURA, nunca escreve):
  * consultas em lote: até 50 produtos por chamada de saldos; o id do produto é resolvido 1x e guardado
  * nunca renova token (o cron renovar_bling_tokens.sh cuida); token vencido => encerra
  * >= 0.6 s entre chamadas; ao primeiro 429/401/403 PARA e tenta de novo na próxima rodada
  * só grava se o valor mudou; se o Bling não devolver linha de saldo do produto, NÃO assume 0 (mantém o valor)
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

from db import db_session

BASE = "https://api.bling.com.br/Api/v3"
PAUSA = 0.6
TIMEOUT = (3, 10)
LOTE = 50


class Parar(Exception):
    pass


class Bling:
    def __init__(self):
        from services.bling_service import BlingService
        self.bs = BlingService()
        self.ultima = 0.0

    def get(self, path, params=None):
        t = self.bs._load_tokens() or {}
        try:
            exp = float(t.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0.0
        if not t.get("access_token") or time.time() > exp - 120:
            raise Parar("token vencido — espere o cron renovar (renovar_bling_tokens.sh)")
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
            raise Parar(f"{r.status_code} do Bling — token/permissão")
        if r.status_code >= 500:
            raise Parar(f"{r.status_code} do Bling")
        return None


def garantir_tabela(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS estoque_cache (
        sku_key TEXT PRIMARY KEY, id_bling BIGINT, atualizado_em BIGINT NOT NULL DEFAULT 0)""")


def skus_pendentes(conn):
    rows = conn.execute("""
        SELECT DISTINCT pr.sku AS sku FROM pedido_produtos pp
        JOIN produtos pr ON pr.id = pp.produto_id
        JOIN pedidos p ON p.id = pp.pedido_id
        WHERE p.status <> 'CONCLUIDO'""").fetchall()
    return [r["sku"] for r in rows if r["sku"]]


def saldo_deposito(row, dep):
    """Saldo físico no depósito dep (0 se o depósito não aparece nos saldos)."""
    for d in row.get("depositos") or []:
        if str(d.get("id")) == str(dep):
            return int(float(d.get("saldoFisico") or 0))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    dep = os.environ.get("BLING_DEPOSITO_ID", "").strip()
    if not dep:
        print("BLING_DEPOSITO_ID não definido (.env / ambiente): sem ele não sei qual depósito usar."); return 2
    with db_session() as conn:
        garantir_tabela(conn)
        skus = [s.strip() for s in a.skus.split(",")] if a.skus else skus_pendentes(conn)
        skus = list(dict.fromkeys(s for s in skus if s))
        ids = {}
        for s in skus:
            r = conn.execute("SELECT id_bling FROM estoque_cache WHERE sku_key=?", (s.lower(),)).fetchone()
            if r and r["id_bling"]:
                ids[s] = int(r["id_bling"])
    t0 = time.time()
    b = Bling()
    mudou = lidos = sem_produto = 0
    try:
        # 1) resolve o id de quem ainda não tem (1x na vida do SKU)
        for s in [x for x in skus if x not in ids]:
            r = b.get("/produtos", {"codigo": s, "limite": 5}) or {}
            p = next((p for p in (r.get("data") or []) if str(p.get("codigo", "")).lower() == s.lower()), None)
            if not p:
                sem_produto += 1
                continue
            ids[s] = int(p["id"])
            with db_session() as conn:
                conn.execute("""INSERT INTO estoque_cache (sku_key, id_bling, atualizado_em) VALUES (?,?,?)
                                ON CONFLICT (sku_key) DO UPDATE SET id_bling=EXCLUDED.id_bling""",
                             (s.lower(), ids[s], int(time.time())))
        # 2) saldos em lote
        por_id = {v: k for k, v in ids.items()}
        pendente = list(por_id)
        for i in range(0, len(pendente), LOTE):
            grupo = pendente[i:i + LOTE]
            r = b.get("/estoques/saldos", [("idsProdutos[]", str(x)) for x in grupo]) or {}
            achados = {}
            for row in r.get("data") or []:
                pid = (row.get("produto") or {}).get("id")
                if pid is not None:
                    achados[int(pid)] = saldo_deposito(row, dep)
            for pid in grupo:
                if pid not in achados:
                    # produto sem linha de saldo no lote: confere sozinho (evita assumir 0 errado)
                    r1 = b.get("/estoques/saldos", [("idsProdutos[]", str(pid))]) or {}
                    rows1 = r1.get("data") or []
                    if rows1:
                        achados[pid] = saldo_deposito(rows1[0], dep)
                    else:
                        print(f"  !! {por_id[pid]}: o Bling não devolveu saldo nenhum; não mexo no valor atual")
            with db_session() as conn:
                for pid, saldo in achados.items():
                    sku = por_id[pid]
                    lidos += 1
                    atual = conn.execute("SELECT estoque_bling FROM produtos WHERE sku=?", (sku,)).fetchone()
                    if atual is None:
                        continue
                    if atual["estoque_bling"] is None or int(atual["estoque_bling"]) != saldo:
                        mudou += 1
                        print(f"  {sku}: {atual['estoque_bling']} -> {saldo}" + ("  (dry)" if a.dry else ""))
                        if not a.dry:
                            conn.execute("UPDATE produtos SET estoque_bling=? WHERE sku=?", (saldo, sku))
    except Parar as e:
        print(f"PAREI: {e}")
        print(f"lidos {lidos}, alterados {mudou} antes de parar")
        return 1
    print(f"ok: {len(skus)} SKU(s), lidos {lidos}, alterados {mudou}, sem produto no Bling {sem_produto}, {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
ESEOF
chmod +x estoque_sync.py
python3 -m py_compile estoque_sync.py && echo ">> estoque_sync.py instalado"
echo ">> teste sem gravar (mostra o que mudaria):"
BLING_DEPOSITO_ID=$DEP venv/bin/python estoque_sync.py --dry 2>&1 | grep -v -i "warn" | tail -40
echo ""
echo "Se o teste acima parece certo, ligue o automatico:  bash instalar_estoque_sync.sh --cron"
exit 0
fi

# --cron
[ -f estoque_sync.py ] || { echo "Rode antes: bash instalar_estoque_sync.sh"; exit 1; }
DIR="$PWD"; LOG="$HOME/nexuswms/estoque_sync.log"; mkdir -p "$HOME/nexuswms"
crontab -l > "$HOME/backups_wms/crontab_antes_estoque_$(date +%Y%m%d_%H%M%S).txt" 2>/dev/null || true
if crontab -l 2>/dev/null | grep -q "estoque_sync.py"; then echo ">> cron ja existe (ok)"; exit 0; fi
( crontab -l 2>/dev/null
  echo "*/5 * * * * cd $DIR && BLING_DEPOSITO_ID=$DEP flock -n /tmp/estoque_sync.lock venv/bin/python estoque_sync.py 2>&1 | grep -v -i warn >> $LOG"
  echo "0 4 * * 0 tail -n 2000 $LOG > $LOG.tmp && mv $LOG.tmp $LOG"
) | crontab -
echo ">> cron ligado: estoque atualiza sozinho a cada 5 minutos. Log: $LOG"
echo "   Desligar: crontab -l | grep -v estoque_sync | crontab -"
