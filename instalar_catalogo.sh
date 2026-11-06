#!/usr/bin/env bash
# Sync COMPLETO do catalogo do Bling (nome + foto) para o cache local. Rode DENTRO da pasta do backend (app.py).
#   bash instalar_catalogo.sh          -> instala o script e deixa o cache valendo 2 dias
#   bash instalar_catalogo.sh --cron   -> roda sozinho todo dia as 03:17
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac

if [ "${1:-}" != "--cron" ]; then
cat > catalogo_sync.py <<'CATEOF'
#!/usr/bin/env python3
"""
Sincroniza o CATÁLOGO INTEIRO do Bling (código, nome, foto) para a tabela mapa_produto_cache.
Depois disso o Mapa, o "Onde está" e o cadastro de posição NÃO batem mais no Bling a cada clique:
tudo sai do banco local. Roda 1x por dia no cron (ou na mão quando quiser).

    python catalogo_sync.py            # tudo (lista paginada; detalhe só pra quem veio sem foto)
    python catalogo_sync.py --sem-detalhe   # só a lista (rápido; produtos sem imagemURL ficam sem foto)
    python catalogo_sync.py --dry      # não grava

Cuidados com o Bling (só LEITURA):
  * nunca renova token (cron renovar_bling_tokens.sh cuida); token vencido => encerra
  * >= 0.4 s entre chamadas; 429 => espera o Retry-After (mín. 60 s) e continua de onde parou
  * grava com consultado_em = agora e achou = 1; nome/foto do mapa_localizacao também são atualizados
"""
from __future__ import annotations

import argparse
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
PAUSA = 0.4
TIMEOUT = (3, 15)
LIMITE = 100


class Parar(Exception):
    pass


class Bling:
    def __init__(self):
        from services.bling_service import BlingService
        self.bs = BlingService()
        self.ultima = 0.0
        self.chamadas = 0

    def get(self, path, params=None):
        for tentativa in range(6):
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
            self.chamadas += 1
            try:
                r = requests.get(BASE + path, params=params, timeout=TIMEOUT,
                                 headers={"Authorization": f"Bearer {t['access_token']}", "Accept": "application/json"})
            except requests.RequestException as e:
                print(f"   rede: {e} — tentando de novo em 20 s", flush=True)
                time.sleep(20)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                try:
                    ra = int(r.headers.get("Retry-After") or 0)
                except ValueError:
                    ra = 0
                seg = max(ra, 60)
                print(f"   429 (limite do Bling) — esperando {seg} s", flush=True)
                time.sleep(seg)
                continue
            if r.status_code in (401, 403):
                raise Parar(f"Bling respondeu {r.status_code} — token/permissão")
            if r.status_code == 404:
                return None
            print(f"   HTTP {r.status_code}: {r.text[:200]} — tentando de novo em 10 s", flush=True)
            time.sleep(10)
        raise Parar("muitas falhas seguidas")


def foto_url(prod: dict):
    if not isinstance(prod, dict):
        return None
    direto = (prod.get("imagemURL") or "").strip()
    if direto:
        return direto
    midia = prod.get("midia") or {}
    imgs = (midia.get("imagens") or {}) if isinstance(midia, dict) else {}
    for grupo in ("externas", "internas"):
        for im in (imgs.get(grupo) or []):
            if isinstance(im, str) and im.strip():
                return im.strip()
            if isinstance(im, dict):
                link = im.get("link") or im.get("linkMiniatura")
                if link:
                    return link
    for im in (imgs.get("imagensURL") or []):
        if isinstance(im, str) and im.strip():
            return im.strip()
        if isinstance(im, dict):
            link = im.get("link") or im.get("url") or im.get("linkMiniatura")
            if link:
                return link
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-detalhe", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    ini = time.time()
    b = Bling()
    produtos = {}          # codigo -> {nome, foto, id}
    pagina = 1
    print(">> lendo o catálogo do Bling…", flush=True)
    while True:
        res = b.get("/produtos", {"pagina": pagina, "limite": LIMITE})
        data = (res or {}).get("data") or []
        if not data:
            break
        for p in data:
            cod = (p.get("codigo") or "").strip()
            if not cod:
                continue
            produtos[cod] = {"nome": p.get("nome") or p.get("descricao"), "foto": foto_url(p), "id": p.get("id")}
        print(f"   página {pagina}: {len(data)} produtos (total {len(produtos)})", flush=True)
        if len(data) < LIMITE:
            break
        pagina += 1

    sem_foto = [c for c, v in produtos.items() if not v["foto"] and v["id"]]
    if not a.sem_detalhe and sem_foto:
        print(f">> {len(sem_foto)} sem foto na lista — buscando o detalhe (1 chamada cada)…", flush=True)
        for i, cod in enumerate(sem_foto, 1):
            try:
                det = b.get(f"/produtos/{produtos[cod]['id']}")
            except Parar:
                raise
            produtos[cod]["foto"] = foto_url((det or {}).get("data") or {})
            if i % 50 == 0:
                print(f"   {i}/{len(sem_foto)}", flush=True)

    com_foto = sum(1 for v in produtos.values() if v["foto"])
    print(f">> {len(produtos)} produtos, {com_foto} com foto, {b.chamadas} chamadas ao Bling, {int(time.time() - ini)} s", flush=True)
    if a.dry:
        return

    agora = int(time.time())
    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mapa_produto_cache (
                sku_key TEXT PRIMARY KEY, nome TEXT, foto TEXT, achou INTEGER NOT NULL, consultado_em BIGINT NOT NULL
            )""")
        n = 0
        for cod, v in produtos.items():
            key = cod[:64].lower()
            conn.execute("""
                INSERT INTO mapa_produto_cache (sku_key, nome, foto, achou, consultado_em) VALUES (?,?,?,?,?)
                ON CONFLICT (sku_key) DO UPDATE SET nome=EXCLUDED.nome,
                    foto=COALESCE(EXCLUDED.foto, mapa_produto_cache.foto), achou=1, consultado_em=EXCLUDED.consultado_em
            """, (key, v["nome"], v["foto"], 1, agora))
            n += 1
        # o mapa também passa a mostrar nome/foto certos
        try:
            conn.execute("""
                UPDATE mapa_localizacao m SET nome = COALESCE(NULLIF(m.nome,''), c.nome), foto = COALESCE(c.foto, m.foto)
                  FROM mapa_produto_cache c WHERE c.sku_key = m.sku_key
            """)
        except Exception as e:
            print(f"   (mapa_localizacao não atualizado: {e})", flush=True)
    print(f">> {n} produtos gravados no cache. Mapa e 'Onde está' agora respondem do banco.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Parar as e:
        print(f"PAROU: {e}", flush=True)
        sys.exit(2)
CATEOF
python3 -m py_compile catalogo_sync.py && echo ">> catalogo_sync.py ok"

# cache do mapa passa a valer 2 dias mesmo sem foto / nao achado (o sync diario renova)
if [ -f mapa_routes.py ]; then
  cp mapa_routes.py mapa_routes.py.bak_catalogo
  sed -i 's/^TTL_SEM_FOTO = .*/TTL_SEM_FOTO = 2 * 86400/; s/^TTL_NAO_ACHOU = .*/TTL_NAO_ACHOU = 2 * 86400/' mapa_routes.py
  python3 -m py_compile mapa_routes.py && echo ">> mapa_routes.py: cache vale 2 dias (backup em mapa_routes.py.bak_catalogo)"
fi
echo ""
echo "Agora rode o sync (demora alguns minutos, pode deixar rodando):"
echo "   venv/bin/python -u catalogo_sync.py"
echo "Depois: sudo systemctl restart wms-russia   (para o mapa usar o cache novo)"
echo "Todo dia sozinho: bash instalar_catalogo.sh --cron"
exit 0
fi

# --cron
[ -f catalogo_sync.py ] || { echo "Rode antes: bash instalar_catalogo.sh"; exit 1; }
DIR="$PWD"; LOG="$HOME/nexuswms/catalogo_sync.log"; mkdir -p "$HOME/nexuswms" "$HOME/backups_wms"
crontab -l > "$HOME/backups_wms/crontab_antes_catalogo_$(date +%Y%m%d_%H%M%S).txt" 2>/dev/null || true
if crontab -l 2>/dev/null | grep -q "catalogo_sync.py"; then echo ">> cron ja existe (ok)"; exit 0; fi
( crontab -l 2>/dev/null
  echo "17 3 * * * cd $DIR && flock -n /tmp/catalogo_sync.lock venv/bin/python catalogo_sync.py 2>&1 | grep -v -i warn >> $LOG"
) | crontab -
echo ">> cron ligado: catalogo sincroniza todo dia 03:17. Log: $LOG"
echo "   Desligar: crontab -l | grep -v catalogo_sync | crontab -"
