# services/bling_service.py
from __future__ import annotations

import os
import json
import time
import base64
import random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from requests.exceptions import Timeout, ConnectionError, RequestException
from services.sku_alias_service import SkuAliasService

import requests
from dotenv import load_dotenv


# =========================
# Helpers
# =========================
def _is_pdf(b: bytes) -> bool:
    return isinstance(b, (bytes, bytearray)) and len(b) >= 4 and b[:4] == b"%PDF"


def _is_html(b: bytes) -> bool:
    if not isinstance(b, (bytes, bytearray)):
        return False
    h = b.lstrip()[:200].lower()
    return h.startswith(b"<!doctype html") or h.startswith(b"<html") or (b"<html" in h)


def _safe_int(v):
    try:
        return int(v)
    except Exception:
        return None


def _find_first_int_key_like(obj, keys_lower: set[str]):
    """Procura valor int/str-int para qualquer chave em keys_lower de forma recursiva."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys_lower:
                iv = _safe_int(v)
                if iv is not None:
                    return int(iv)
            found = _find_first_int_key_like(v, keys_lower)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_first_int_key_like(v, keys_lower)
            if found is not None:
                return found
    return None


class BlingService:
    """
    Bling API v3 (OAuth2) com token salvo em arquivo (bling_tokens.json na raiz do projeto).

    Objetivos:
      - Achar PV pelo numeroLoja (ML order id)
      - Achar NF-e correta vinculada ao PV/numeroLoja (regra de vínculo forte)
      - Baixar o DANFE oficial (preferindo simplificado quando existir)
      - Etiquetas via /logisticas/etiquetas (ZPL/PDF) quando houver
      - Estoque e kits
    """

    def __init__(self, token_file: str = "bling_tokens.json"):
        load_dotenv()

        self.client_id = os.getenv("BLING_CLIENT_ID")
        self.client_secret = os.getenv("BLING_CLIENT_SECRET")
        self.base_url = (os.getenv("BLING_BASE_URL") or "https://api.bling.com.br/Api/v3").rstrip("/")

        if not self.client_id or not self.client_secret:
            raise Exception("❌ BLING_CLIENT_ID/BLING_CLIENT_SECRET não configurados no .env")

        # raiz do projeto (um nível acima de /services)
        self.token_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), token_file)

        # cache de kit
        self._kit_cache: dict[str, tuple[bool, str, list[dict]]] = {}

    # ------------------------
    # TOKENS
    # ------------------------
    def _load_tokens(self):
        with open(self.token_file, "r", encoding="utf-8") as f:
            return json.load(f)


    def _save_tokens(self, tokens: dict):
        tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(tokens, f, ensure_ascii=False, indent=2)

    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return base64.b64encode(raw).decode("utf-8")

    def _refresh_access_token(self, refresh_token: str) -> str:
        url = "https://api.bling.com.br/Api/v3/oauth/token"
        headers = {
            "Authorization": f"Basic {self._basic_auth()}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        r = requests.post(url, headers=headers, data=data, timeout=(10, 60))
        if r.status_code != 200:
            raise Exception(f"❌ Bling refresh_token falhou: {r.status_code} - {r.text}")
        tokens = r.json()
        self._save_tokens(tokens)
        return tokens.get("access_token")

    def get_access_token(self) -> str:
        tokens = self._load_tokens()
        if not tokens:
            raise Exception("❌ Tokens do Bling não encontrados (bling_tokens.json).")

        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        expires_at = float(tokens.get("expires_at", time.time() + tokens.get("expires_in", 3600)))

        # renova faltando 5 minutos
        if time.time() > (expires_at - 300):
            if not refresh:
                raise Exception("❌ Refresh token ausente.")
            print("🔄 Renovando access_token Bling...", flush=True)
            self._refresh_access_token(refresh)
            tokens = self._load_tokens() or {}
            access = tokens.get("access_token")

        if not access:
            raise Exception("❌ access_token ausente no bling_tokens.json")
        return access

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json"
        }

    # ------------------------
    # HTTP helpers (com retry 429)
    # ------------------------
    def _request(self, method: str, path: str, *, params=None, json=None, data=None, timeout=(10, 30)):
        url = path if path.startswith("http") else f"{self.base_url}{path}"

        wait = 0.6
        last_exc = None
        last_resp = None

        if (os.getenv("BLING_DEBUG") or "").strip() in ("1", "true", "True", "yes", "YES"):
            print(f"🌐 BLING {method} {url} params={params}", flush=True)

        for _ in range(10):
            try:
                r = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json,
                    data=data,
                    timeout=timeout,
                )
                last_resp = r

                if r.status_code == 429 or (500 <= r.status_code <= 599):
                    time.sleep(wait + random.uniform(0, 0.25))
                    wait = min(wait * 1.7, 5.0)
                    continue

                return r

            except (Timeout, ConnectionError) as e:
                last_exc = e
                time.sleep(wait + random.uniform(0, 0.25))
                wait = min(wait * 1.7, 5.0)
                continue
            except RequestException as e:
                last_exc = e
                break

        if last_resp is not None:
            return last_resp
        raise Exception(f"❌ Bling request falhou após retries: {method} {url} err={last_exc}")

    def _get(self, path: str, *, params=None, timeout=(10, 90)):
        return self._request("GET", path, params=params, timeout=timeout)

    def _post(self, path: str, *, params=None, json=None, data=None, timeout=(10, 90)):
        return self._request("POST", path, params=params, json=json, data=data, timeout=timeout)

    def _put(self, path: str, *, params=None, json=None, data=None, timeout=(10, 90)):
        return self._request("PUT", path, params=params, json=json, data=data, timeout=timeout)

    def _get_json(self, path: str, params: dict | None = None, timeout=(10, 90)) -> dict:
        r = self._get(path, params=params, timeout=timeout)
        if r.status_code != 200:
            raise Exception(f"❌ Bling GET falhou: {r.status_code} - {r.text}")
        return r.json() or {}

    def _download(self, url: str) -> bytes:
        if not url:
            return b""
        r = self._request("GET", url, timeout=(10, 120))
        if r.status_code != 200:
            raise Exception(f"❌ Download falhou: {r.status_code} - {r.text[:300]}")
        return r.content or b""

    # ============================================================
    # ESTOQUE (API v3)
    # ============================================================
    _estoque_cache: dict = {}

    def obter_estoque(self, sku: str) -> int | None:
        sku_in = (sku or "").strip()
        if not sku_in:
            return None

        # ✅ Só resolve alias se parecer ID do Mercado Livre (ex: "MLB123...")
        sku_api = sku_in
        try:
            if sku_in.upper().startswith("MLB"):
                alias = SkuAliasService().get(sku_in)  # ml_item_id -> sku_bling
                if alias:
                    sku_api = str(alias).strip()
        except Exception:
            sku_api = sku_in

        now = time.time()

        # ✅ cache por sku_api (o que realmente consultamos)
        c = self._estoque_cache.get(sku_api)
        if c and (now - c.get("ts", 0) < 15):
            return c.get("val")

        def pick_id_from_list(data: dict) -> int | None:
            itens = data.get("data") or []
            if not isinstance(itens, list) or not itens:
                return None
            for p in itens:
                if not isinstance(p, dict):
                    continue
                cod = str(p.get("codigo") or p.get("sku") or "").strip()
                if cod == sku_api:
                    pid = p.get("id")
                    return int(pid) if pid else None
            pid = itens[0].get("id")
            return int(pid) if pid else None

        try:
            # 1) acha produto_id
            data = self._get_json("/produtos", params={"codigo": sku_api})
            produto_id = pick_id_from_list(data)

            if not produto_id:
                data = self._get_json("/produtos", params={"criterio": sku_api, "limite": 100, "pagina": 1})
                produto_id = pick_id_from_list(data)

            if not produto_id:
                self._estoque_cache[sku_api] = {"ts": now, "val": 0}
                return 0

            # 2) busca saldos
            saldos = self._get_json("/estoques/saldos", params=[("idsProdutos[]", str(int(produto_id)))])

            if (os.getenv("BLING_DEBUG") or "").strip() in ("1", "true", "True", "yes", "YES"):
                import json
                print("===== SALDOS RAW =====")
                print(json.dumps(saldos, indent=2, ensure_ascii=False))
                print("======================")

            rows = saldos.get("data") or []
            if not isinstance(rows, list):
                rows = []

            # 3) estoque real: soma de depósitos (fallback do total)
            val = 0

            for row in rows:
                if not isinstance(row, dict):
                    continue

                depositos = row.get("depositos") or []
                if isinstance(depositos, list) and depositos:
                    soma = 0
                    for d in depositos:
                        if not isinstance(d, dict):
                            continue
                        try:
                            soma += int(d.get("saldoFisico") or 0)
                        except Exception:
                            pass
                    val = soma
                    break

                # se não vier depósitos, usa o total
                total = row.get("saldoFisicoTotal")
                if isinstance(total, (int, float)):
                    val = int(total)
                    break

            val = int(val or 0)

            # ✅ salva cache certo (sku_api)
            self._estoque_cache[sku_api] = {"ts": now, "val": val}
            return val

        except Exception as e:
            if (os.getenv("BLING_DEBUG") or "").strip() in ("1", "true", "True", "yes", "YES"):
                print(f"❌ obter_estoque falhou sku={sku_in} (api={sku_api}): {e}", flush=True)
            return None

    # ============================================================
    # KIT / ESTRUTURA (API v3)
    # ===========================================================

    def resolver_pv_por_codigo_digitado(ml_service, bling_service, codigo_digitado: str):
        print("\n🔎 TRADUZINDO ID DO MERCADO LIVRE...")
        numero_loja = ml_service.resolver_ml_para_numero_loja(codigo_digitado)

        if not numero_loja:
            print("❌ Não consegui converter o código do ML")
            return None

        print("🔎 PROCURANDO PV NO BLING...")
        pv_id = bling_service.buscar_pedido_venda_id_por_numero_loja(numero_loja)

        if not pv_id:
            print("⏳ Pedido ainda sincronizando no Bling...")
            return None

        return pv_id

    # ------------------------
    # Find helpers
    # ------------------------
    def _find_first_url_containing(self, obj: Any, needle: str) -> str | None:
        """Varre recursivamente dict/list e retorna a primeira string URL que contém needle."""
        try:
            if isinstance(obj, dict):
                for _, v in obj.items():
                    found = self._find_first_url_containing(v, needle)
                    if found:
                        return found
            elif isinstance(obj, list):
                for v in obj:
                    found = self._find_first_url_containing(v, needle)
                    if found:
                        return found
            elif isinstance(obj, str):
                s = obj.strip()
                if s.startswith("http") and needle.lower() in s.lower():
                    return s
        except Exception as e:
            print(f"❌ obter_estoque ERRO sku={sku}: {e}", flush=True)
            raise

    def _find_first_key_like(self, obj: Any, keys_lower: set[str]):
        """Procura valor de qualquer chave em keys_lower de forma recursiva."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in keys_lower and isinstance(v, str) and v.strip():
                    return v.strip()
                found = self._find_first_key_like(v, keys_lower)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = self._find_first_key_like(v, keys_lower)
                if found:
                    return found
        return None

    # ------------------------
    # PV (resolver por ML order id)
    # ------------------------
    def _extract_skus_from_pv_detail(self, pv_detail: dict) -> list[str]:
        """
        Extrai SKUs (códigos) do PV detalhe.
        Funciona com formato {"data": {...}} ou direto {...}
        """
        data = pv_detail.get("data") if isinstance(pv_detail, dict) and "data" in pv_detail else pv_detail
        if not isinstance(data, dict):
            return []

        itens = data.get("itens") or []
        out = []

        def pick_sku(item_obj: dict) -> str | None:
            if not isinstance(item_obj, dict):
                return None
            # tenta padrões comuns
            sku = (
                item_obj.get("codigo") or item_obj.get("sku") or item_obj.get("codigoProduto") or item_obj.get("codigo_produto")
                or ((item_obj.get("produto") or {}).get("codigo") if isinstance(item_obj.get("produto"), dict) else None)
            )
            return str(sku).strip() if sku else None

        for it in itens if isinstance(itens, list) else []:
            obj = it.get("item") if isinstance(it, dict) and isinstance(it.get("item"), dict) else it
            sku = pick_sku(obj if isinstance(obj, dict) else {})
            if sku:
                out.append(sku)

        return [s for s in out if s]

    def achar_pv_do_mercadolivre(self, ml_order_id: str):
        """
        Resolve o pedido do Mercado Livre dentro do Bling
        usando numeroLoja (integração oficial).
        """

        numero = str(ml_order_id).strip()

        pagina = 1
        limite = 100

        while True:

            params = {
                "criterio": numero,
                "pagina": pagina,
                "limite": limite,
            }

            print(f"🔎 Procurando PV no Bling pelo numeroLoja={numero} pag={pagina}")

            data = self._get_json("/pedidos/vendas", params=params)
            itens = data.get("data") or []

            if not itens:
                return None

            for pv in itens:
                if str(pv.get("numeroLoja")) == numero:
                    print("✅ ENCONTRADO PV:", pv["id"])
                    return pv["id"]

            if len(itens) < limite:
                return None

            pagina += 1
            
    def resolver_pv_com_retry(bling, ml_id, tentativas=10):
        for i in range(tentativas):
            pv = bling.achar_pv_por_numero_loja(ml_id)
            if pv:
                return pv

            print("⌛ aguardando sincronização Bling...")
            time.sleep(3)

        return None

    def achar_pv_por_numero_loja(self, ml_pack_id: str):

        numero = str(ml_pack_id).strip()

        pagina = 1
        limite = 100

        while True:
            params = {
                "criterio": numero,
                "pagina": pagina,
                "limite": limite,
            }

            data = self._get_json("/pedidos/vendas", params=params)
            itens = data.get("data") or []

            if not itens:
                return None

            for pv in itens:
                if str(pv.get("numeroLoja")) == numero:
                    print("✅ PV ENCONTRADO:", pv["id"])
                    return pv["id"]

            if len(itens) < limite:
                return None

            pagina += 1

    def classificar_erro_emissao_nfe(self, err: Exception | str) -> str:
        s = str(err or "").lower()

        # faltas comuns que indicam “precisa UI / cadastro”
        chaves_manual = [
            "numeroDocumento".lower(),
            "cpf", "cnpj",
            "endereco", "logradouro", "bairro", "municipio", "cep", "uf",
            "natureza", "naturezaoperacao",
            "ncm", "cfop", "cst", "csosn", "origem",
            "inscricao", "ie",
            "transport", "frete",
            "rejeit", "sefaz",  # rejeição por regra fiscal (muitas vezes é cadastro)
        ]

        if any(k in s for k in chaves_manual):
            return "NEEDS_ROBOT_OR_MANUAL"

        # rate limit / instabilidade -> pode retentar
        if "429" in s or "timeout" in s or "connection" in s or "5" in s:
            return "TRANSIENT"

        return "UNKNOWN"

    def resolver_pv_id_por_ml_id(self, ml_service, any_ml_id: str, *, dias: int = 15) -> int | None:
        """
        Resolve PV no Bling a partir de um ID digitado (order ou pack do ML).
        Ordem:
        1) PACK->ORDER e tenta numeroLoja (rápido)
        2) tenta achar por shipping.id dentro do PV (mais certeiro)
        3) tenta achar por buyer.nickname (contato) e valida no DETALHE
        """
        import json
        from datetime import datetime, timedelta

        # 0) normaliza pra ORDER (se vier pack)
        order_id = ml_service.resolver_ml_para_numero_loja(str(any_ml_id))
        if not order_id:
            return None

        # 1) tentativa rápida: numeroLoja == ORDER
        pv_id = self.buscar_pedido_venda_id_por_numero_loja(order_id, somente_em_aberto=False)
        if pv_id:
            return int(pv_id)

        # 2) pega dados do pedido no ML (pra usar shipping e nickname)
        order = ml_service.obter_pedido(order_id)
        shipping_id = (order.get("shipping") or {}).get("id")
        nickname = ((order.get("buyer") or {}).get("nickname") or "").strip()

        dt_ini = (datetime.now() - timedelta(days=int(dias))).strftime("%Y-%m-%d 00:00:00")

        def dump_has(obj, needle: str) -> bool:
            if not needle:
                return False
            try:
                return needle in json.dumps(obj, ensure_ascii=False)
            except Exception:
                return needle in str(obj)

        # helper: varre páginas “recentes” e valida no DETALHE (sem depender do list row)
        def scan_by_criterio(criterio: str, max_paginas: int = 10) -> int | None:
            if not criterio:
                return None
            limite = 100
            for pagina in range(1, max_paginas + 1):
                data = self._get_json("/pedidos/vendas", params={
                    "criterio": criterio,
                    "pagina": pagina,
                    "limite": limite,
                    "dataAlteracaoInicial": dt_ini,
                }, timeout=(10, 30))

                itens = data.get("data") or []
                if not itens:
                    break

                for row in itens:
                    pid = (row or {}).get("id")
                    if not pid:
                        continue
                    det = self.get_pedido_venda_detalhe(int(pid))
                    # valida por: order_id / shipping_id / nickname
                    if dump_has(det, str(order_id)) or dump_has(det, str(shipping_id)) or (nickname and dump_has(det, nickname)):
                        return int(pid)

                if len(itens) < limite:
                    break
            return None

        # 2a) mais forte: procurar pelo SHIPPING ID (normalmente é único)
        if shipping_id:
            found = scan_by_criterio(str(shipping_id), max_paginas=10)
            if found:
                return int(found)

        # 2b) fallback: procurar pelo nickname (contato do Bling geralmente tem "(nickname)")
        if nickname:
            found = scan_by_criterio(nickname, max_paginas=10)
            if found:
                return int(found)

        # 3) último fallback: varrer recentes e procurar order_id “em qualquer campo”
        found = self.buscar_pv_id_por_ml_id_em_recentes(str(order_id), dias=dias, max_paginas=20)
        if found:
            return int(found)

        return None

    def buscar_pv_id_por_ml_id_em_recentes(self, ml_id: str, *, dias: int = 10, max_paginas: int = 20) -> int | None:
        ml_id = str(ml_id or "").strip()
        if not ml_id:
            return None

        from datetime import datetime, timedelta
        dt_ini = (datetime.now() - timedelta(days=int(dias))).strftime("%Y-%m-%d 00:00:00")

        limite = 100
        for pagina in range(1, max_paginas + 1):
            data = self._get_json("/pedidos/vendas", params={
                "pagina": pagina,
                "limite": limite,
                "dataAlteracaoInicial": dt_ini,
            }, timeout=(10, 30))

            itens = data.get("data") or []
            if not itens:
                break

            for pv in itens:
                if not isinstance(pv, dict):
                    continue

                # 1) tenta no “row” mesmo
                try:
                    if ml_id in json.dumps(pv, ensure_ascii=False):
                        pid = pv.get("id")
                        return int(pid) if pid else None
                except Exception:
                    pass

                # 2) tenta no DETALHE (mais caro, mas mais certeiro)
                pid = pv.get("id")
                if not pid:
                    continue
                det = self.get_pedido_venda_detalhe(int(pid))
                try:
                    if ml_id in json.dumps(det, ensure_ascii=False):
                        return int(pid)
                except Exception:
                    pass

            if len(itens) < limite:
                break

        return None

    def buscar_pvs_por_sku(
        self,
        sku: str,
        *,
        dias: int = 30,
        max_paginas: int = 5,
    ) -> list[dict]:

        sku = str(sku or "").strip()
        if not sku:
            return []

        from datetime import datetime, timedelta

        dt_ini = (datetime.now() - timedelta(days=int(dias))).strftime("%Y-%m-%d 00:00:00")

        limite = 100
        results = []

        for pagina in range(1, max_paginas + 1):

            print(f"🔎 Buscando pagina={pagina}", flush=True)

            params = {
                "pagina": pagina,
                "limite": limite,
                "criterio": sku,  # 🔥 FILTRO IMPORTANTE
                "dataAlteracaoInicial": dt_ini,
            }

            data = self._get_json("/pedidos/vendas", params=params, timeout=(10, 30))
            itens = data.get("data") or []

            if not itens:
                break

            for pv_row in itens:
                pv_id = pv_row.get("id")
                if not pv_id:
                    continue

                pv_detail = self.get_pedido_venda_detalhe(pv_id)
                skus = self._extract_skus_from_pv_detail(pv_detail)

                if sku in skus:
                    obj = pv_detail.get("data") or pv_detail

                    results.append({
                        "pv_id": pv_id,
                        "numeroLoja": obj.get("numeroLoja"),
                        "data": obj.get("data") or obj.get("dataVenda"),
                        "skus": skus,
                    })

            if len(itens) < limite:
                break

        return results

    def resolver_pv_por_ml_id_via_skus(self, ml_service, ml_order_id: str, *, dias_busca: int = 15) -> list[dict]:
        """
        Retorna candidatos de PV no Bling usando SKUs do pedido no Mercado Livre.
        """
        ml_order_id = str(ml_order_id or "").strip()
        if not ml_order_id:
            return []

        # você adapta isso pro SEU MercadoLivreService:
        order = ml_service.get_order(ml_order_id)  # <-- exemplo
        itens = order.get("order_items") or []

        skus_ml = []
        for it in itens:
            # exemplos comuns:
            sku = (
                (it.get("item") or {}).get("seller_sku")
                or it.get("seller_sku")
                or it.get("sku")
            )
            if sku:
                skus_ml.append(str(sku).strip())

        skus_ml = [s for s in skus_ml if s]
        if not skus_ml:
            return []

        # busca por SKU no Bling e junta tudo
        candidatos = []
        for sku in skus_ml:
            candidatos.extend(self.buscar_pvs_por_sku(sku, dias=dias_busca))

        # rank simples: PV que bate mais SKUs do ML sobe
        score = {}
        for c in candidatos:
            pid = c["pv_id"]
            pv_skus = set(c.get("skus") or [])
            hits = len(pv_skus.intersection(set(skus_ml)))
            if pid not in score or hits > score[pid]["hits"]:
                score[pid] = {**c, "hits": hits}

        out = sorted(score.values(), key=lambda x: x.get("hits", 0), reverse=True)
        return out

    def buscar_pv_id_por_ml_em_qualquer_campo(self, ml_order_id: str, max_paginas: int = 50) -> int | None:
        ml_order_id = str(ml_order_id or "").strip()
        if not ml_order_id:
            return None

        for pagina in range(1, max_paginas + 1):
            data = self._get_json("/pedidos/vendas", params={"pagina": pagina, "limite": 100}, timeout=(10, 30))
            itens = data.get("data") or []
            if not itens:
                break

            for pv in itens:
                if not isinstance(pv, dict):
                    continue
                try:
                    dump = json.dumps(pv, ensure_ascii=False)
                except Exception:
                    dump = str(pv)

                if ml_order_id in dump:
                    pid = pv.get("id")
                    return int(pid) if pid else None

            if len(itens) < 100:
                break

        return None


    def buscar_pedido_venda_id_por_numero_loja(
        self,
        numero_loja: str,
        *,
        somente_em_aberto: bool = False,
    ) -> int | None:
        """
        Busca PV pelo numeroLoja de forma robusta.
        """

        import json

        numero_loja = str(numero_loja or "").strip()
        if not numero_loja:
            return None

        def only_digits(s: str) -> str:
            return "".join(ch for ch in str(s or "") if ch.isdigit())

        alvo_full = numero_loja
        alvo_digits = only_digits(numero_loja)

        limite = 100
        max_paginas = 30

        print(f"🔎 [BLING] Procurando PV numeroLoja={numero_loja}", flush=True)

        for pagina in range(1, max_paginas + 1):

            params = {
                "criterio": alvo_full,
                "pagina": pagina,
                "limite": limite,
            }

            if somente_em_aberto:
                params["situacao"] = 0

            data = self._get_json("/pedidos/vendas", params=params, timeout=(10, 30))
            itens = data.get("data") or []

            if not itens:
                break

            for pv in itens:
                if not isinstance(pv, dict):
                    continue

                # match direto
                if str(pv.get("numeroLoja") or "").strip() == alvo_full:
                    print("✅ PV ENCONTRADO (match direto)", flush=True)
                    return int(pv.get("id"))

                # match por dígitos (tolerante)
                dump = ""
                try:
                    dump = json.dumps(pv, ensure_ascii=False)
                except Exception:
                    dump = str(pv)

                if alvo_digits and alvo_digits in only_digits(dump):
                    print("✅ PV ENCONTRADO (match por dígitos)", flush=True)
                    return int(pv.get("id"))

            if len(itens) < limite:
                break

        print("❌ PV não encontrado no Bling", flush=True)
        return None

        def match_numero_loja(pv: dict) -> bool:
            candidates = [
                pv.get("numeroLoja"),
                pv.get("numero_loja"),
                pv.get("pedidoLoja"),
                pv.get("numeroPedidoCompra"),
                pv.get("observacoes"),
                pv.get("observacoesInternas"),
            ]

            # match exato
            for c in candidates:
                if str(c or "").strip() == alvo_full:
                    return True

            # match por dígitos (tolerante)
            dump = ""
            try:
                dump = json.dumps(pv, ensure_ascii=False)
            except Exception:
                dump = str(pv)

            if alvo_digits:
                for c in candidates:
                    if alvo_digits and alvo_digits in only_digits(str(c or "")):
                        return True
                if alvo_digits in only_digits(dump):
                    return True

            return False

        for base_params in base_params_list:
            for pagina in range(1, max_paginas + 1):
                params = dict(base_params)
                params["pagina"] = pagina
                params["limite"] = limite

                data = self._get_json("/pedidos/vendas", params=params, timeout=(10, 30))
                itens = data.get("data") or []
                if not itens:
                    break

                for pv in itens:
                    if isinstance(pv, dict) and match_numero_loja(pv):
                        pid = pv.get("id")
                        return int(pid) if pid else None

                if len(itens) < limite:
                    break

        return None

    def _match(a: str | None, b: str) -> bool:
        a = "".join(ch for ch in str(a or "").strip() if ch.isdigit())
        b = "".join(ch for ch in str(b or "").strip() if ch.isdigit())
        if not a or not b:
            return False

        if a == b:
            return True

        # tolerância: Bling pode ter guardado só o final (ex: 12 dígitos)
        for n in (12, 10, 9, 8):
            if len(b) >= n and a.endswith(b[-n:]):
                return True

        # se veio com prefixo/sufixo
        return a in b or b in a

        def _try_list(params: dict) -> int | None:
            limite = 100
            for pagina in range(1, 11):
                p = dict(params)
                p.update({"pagina": pagina, "limite": limite})
                data = self._get_json("/pedidos/vendas", params=p, timeout=(10, 30))
                itens = data.get("data") or []
                if not itens:
                    return None

                for pv in itens:
                    if not isinstance(pv, dict):
                        continue
                    nlpv = pv.get("numeroLoja") or pv.get("numero_loja") or pv.get("pedidoLoja")
                    if _match(nlpv, numero_loja):
                        pid = pv.get("id")
                        return int(pid) if pid else None

                if len(itens) < limite:
                    return None
            return None

        # 1) filtros “certos” (o Bling costuma ter algo equivalente a “números de pedidos nas lojas”)
        # (vários nomes porque a API/SDKs variam)
        for key in (
            "numerosPedidosLojas", "numeros_pedidos_lojas", "numerosPedidosLoja",
            "numeroLoja", "numero_loja"
        ):
            try:
                pid = _try_list({key: numero_loja})
                if pid:
                    return pid
            except Exception:
                pass

        # 2) sua busca atual por "criterio" (mantém como fallback)
        try:
            pid = _try_list({"criterio": numero_loja})
            if pid:
                return pid
        except Exception:
            pass

        # 3) fallback final: varre por alteração desde MUITO antigo (pra não perder pedido velho)
        try:
            limite = 100
            max_paginas = 20
            data_ini = "2010-01-01 00:00:00"
            for pagina in range(1, max_paginas + 1):
                params = {
                    "pagina": pagina,
                    "limite": limite,
                    "dataAlteracaoInicial": data_ini,
                }
                data = self._get_json("/pedidos/vendas", params=params, timeout=(10, 30))
                itens = data.get("data") or []
                if not itens:
                    break

                for pv in itens:
                    if not isinstance(pv, dict):
                        continue
                    nlpv = pv.get("numeroLoja") or pv.get("numero_loja") or pv.get("pedidoLoja")
                    if _match(nlpv, numero_loja):
                        pid = pv.get("id")
                        return int(pid) if pid else None

                if len(itens) < limite:
                    break
        except Exception:
            pass

        return None

    def get_pedido_venda_detalhe(self, pv_id: int) -> dict:
        return self._get_json(f"/pedidos/vendas/{int(pv_id)}")

    def obter_skus_do_pedido_venda(self, pv_id: int) -> list[str]:
        """Extrai SKUs do PV (tenta vários formatos)."""
        resp = self._get_json(f"/pedidos/vendas/{int(pv_id)}")
        data = resp.get("data") or resp

        itens = (data.get("itens") if isinstance(data, dict) else None) or []
        out: list[str] = []

        def pick_sku(d: dict) -> str | None:
            if not isinstance(d, dict):
                return None
            return (
                (d.get("codigo") or d.get("sku") or d.get("codigoProduto") or d.get("codigo_produto"))
                or ((d.get("produto") or {}).get("codigo") if isinstance(d.get("produto"), dict) else None)
            )

        for it in itens if isinstance(itens, list) else []:
            if isinstance(it, dict) and "item" in it and isinstance(it["item"], dict):
                sku = pick_sku(it["item"])
            else:
                sku = pick_sku(it if isinstance(it, dict) else {})
            if sku:
                out.append(str(sku).strip())

        return [s for s in out if s]

    def resolver_nfe_por_pv_id(self, pv_id: int, numero_loja: str | None = None) -> int | None:
        pv_id = int(pv_id)

        # 1) melhor: NF dentro do PV
        try:
            nid = self.extrair_nfe_id_do_pedido_venda(pv_id)
            if nid:
                return int(nid)
        except Exception:
            pass

        # 2) fallback: busca por score
        try:
            nid = self.encontrar_nfe_id_por_pedido_venda(pv_id, numero_loja=numero_loja)
            if nid:
                return int(nid)
        except Exception:
            pass

        return None

    def resolver_pv_e_nfe_por_ml_order_id(self, ml_order_id: str) -> tuple[int | None, int | None]:
        ml_order_id = str(ml_order_id or "").strip()
        if not ml_order_id:
            print("resolver_pv_e_nfe_por_ml_order_id: ml_order_id vazio", flush=True)
            return None, None

        pv_id = self.buscar_pedido_venda_id_por_numero_loja(ml_order_id)
        if not pv_id:
            print(f"resolver: nenhum PV encontrado para numeroLoja={ml_order_id}", flush=True)
            return None, None

        pv_id = int(pv_id)
        print(f"resolver: ML order id={ml_order_id} -> PV id={pv_id}", flush=True)

        # ✅ 1) PRIORIDADE MÁXIMA: NF-e que está dentro do PV
        try:
            nfe_from_pv = self.extrair_nfe_id_do_pedido_venda(pv_id)
            if nfe_from_pv:
                nfe_from_pv = int(nfe_from_pv)
                # opcional: loga situação
                try:
                    det = self._get_json(f"/nfe/{nfe_from_pv}")
                    obj = det.get("data") or det
                    situ = obj.get("situacao") if isinstance(obj, dict) else None
                    if situ is None and isinstance(obj, dict):
                        situ = obj.get("status")
                    print(f"resolver: NF-e do PV encontrada id={nfe_from_pv} situacao={situ}", flush=True)
                except Exception:
                    print(f"resolver: NF-e do PV encontrada id={nfe_from_pv}", flush=True)
                return pv_id, nfe_from_pv
        except Exception as e:
            print(f"resolver: falha ao extrair NF-e do PV: {e}", flush=True)

        # ✅ 2) Se PV não tem NF-e, aí sim tenta achar por busca/score
        nfe_id = self.encontrar_nfe_id_por_pedido_venda(pv_id, numero_loja=ml_order_id)
        if nfe_id:
            print(f"resolver: NF-e por busca/score id={int(nfe_id)}", flush=True)
            return pv_id, int(nfe_id)

        print(f"resolver: nenhum candidato NF-e confirmado para PV={pv_id}", flush=True)
        return pv_id, None

    def extrair_nfe_id_do_pedido_venda(self, pv_id: int) -> int | None:
        pv = self.get_pedido_venda_detalhe(int(pv_id))
        if not pv:
            return None

        keys = {"idnotafiscal", "idnota", "idnf", "idnfe", "nfeid", "id_nf", "id_nfe"}
        nid = _find_first_int_key_like(pv, {k.lower() for k in keys})
        if nid:
            return int(nid)
        return None

    # ------------------------
    # SERIAL RULES (config via .env)
    # ------------------------
    def _serial_required_skus(self) -> set[str]:
        raw = (os.getenv("SERIAL_REQUIRED_SKUS") or "").strip()
        if not raw:
            return set()
        return {x.strip() for x in raw.split(",") if x.strip()}

    def _serial_required_prefixes(self) -> list[str]:
        raw = (os.getenv("SERIAL_REQUIRED_PREFIXES") or "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def pedido_venda_exige_serial(self, pv_id: int) -> bool:
        """
        Decide se o PV exige serial baseado em SKU/prefixo.
        (Regra prática pra operação; ANVISA "de verdade" vira cadastro depois)
        """
        skus = self.obter_skus_do_pedido_venda(int(pv_id))
        if not skus:
            return False

        required = self._serial_required_skus()
        prefixes = self._serial_required_prefixes()

        for sku in skus:
            s = str(sku).strip()
            if not s:
                continue
            if s in required:
                return True
            for p in prefixes:
                if s.startswith(p):
                    return True
        return False


    # ------------------------
    # NF-e (encontrar NF correta)
    # ------------------------
    def _listar_nfes(self, criterio: str, pagina=1, limite=50) -> list[dict]:
        data = self._get_json("/nfe", params={"criterio": str(criterio), "pagina": pagina, "limite": limite})
        itens = data.get("data") or []
        return [x for x in itens if isinstance(x, dict)]
        
    def obter_numero_nota(self, nfe_id: int) -> str | None:
        nf = self.obter_nfe_detalhe(int(nfe_id))
        numero = nf.get("numero") or nf.get("numeroNota") or nf.get("numero_nota")
        if numero:
            return str(numero).strip()
        return None

    def _score_nfe_match(self, nfe_row: dict, bling_pv_id: int, numero_loja: str | None) -> int:
        score = 0
        try:
            dump = json.dumps(nfe_row, ensure_ascii=False)
        except Exception:
            dump = str(nfe_row)

        if numero_loja and numero_loja in dump:
            score += 200
        if str(bling_pv_id) in dump:
            score += 120

        for k in ("idPedidoVenda", "pedidoVenda", "pedido_venda", "idVenda", "idsVendas"):
            v = nfe_row.get(k)
            if v is None:
                continue
            if isinstance(v, (int, str)) and str(v) == str(bling_pv_id):
                score += 300
            if isinstance(v, list) and str(bling_pv_id) in [str(x) for x in v]:
                score += 300

        st_raw = nfe_row.get("situacao") if nfe_row.get("situacao") is not None else nfe_row.get("status")
        st = str(st_raw or "").strip().lower()
        if "autoriz" in st:
            score += 10
        if "rejeit" in st or "cancel" in st:
            score -= 50

        return score

    def encontrar_nfe_id_por_pedido_venda(self, bling_pv_id: int, numero_loja: str | None = None) -> int | None:
        bling_pv_id = int(bling_pv_id)
        numero_loja = str(numero_loja).strip() if numero_loja else None

        tentativas: list[str] = []
        if numero_loja:
            tentativas.append(numero_loja)
        tentativas.append(str(bling_pv_id))

        best_id = None
        best_score = -1
        for criterio in tentativas:
            try:
                itens = self._listar_nfes(criterio)
            except Exception:
                continue
            for it in itens:
                nid = _safe_int(it.get("id") or it.get("nfeId") or it.get("idNotaFiscal"))
                if not nid:
                    continue
                sc = self._score_nfe_match(it, bling_pv_id=bling_pv_id, numero_loja=numero_loja)
                if sc > best_score:
                    best_score = sc
                    best_id = nid

        return int(best_id) if best_id else None

    # ============================================================
    # ✅ PATCH JUNTADO AQUI: NF-e status / autorização
    # ============================================================
    def obter_nfe_detalhe(self, nfe_id: int) -> dict:
        det = self._get_json(f"/nfe/{int(nfe_id)}")
        return det.get("data") or det

    def nfe_esta_autorizada(self, nfe_id: int) -> bool:
        """
        Retorna True se a NF-e estiver autorizada (ou equivalente).
        O Bling às vezes retorna 'situacao'/'status' como string ou código.
        """
        obj = self.obter_nfe_detalhe(int(nfe_id))
        raw = obj.get("situacao") if obj.get("situacao") is not None else obj.get("status")

        if isinstance(raw, (int, float)):
            code = int(raw)
            # seus códigos já usados no poll; mantive os mesmos
            return code in {5, 6, 7}

        st = str(raw or "").strip().lower()
        if "autoriz" in st:
            return True
        if "cancel" in st or "rejeit" in st or "deneg" in st:
            return False

        return False

    def resolver_pv_nfe_e_autorizacao_por_ml_order_id(self, ml_order_id: str) -> tuple[int | None, int | None, bool]:
        """
        Helper pra seu fluxo:
          - resolve PV e NF-e vinculada
          - retorna se a NF-e está autorizada
        """
        pv_id, nfe_id = self.resolver_pv_e_nfe_por_ml_order_id(ml_order_id)
        if not pv_id or not nfe_id:
            return pv_id, nfe_id, False
        try:
            ok = self.nfe_esta_autorizada(int(nfe_id))
            return pv_id, nfe_id, bool(ok)
        except Exception:
            return pv_id, nfe_id, False

    # ------------------------
    # DANFE oficial (preferir simplificado)
    # ------------------------

    def _bling_error_code(self, r_text: str) -> int | None:
        try:
            j = json.loads(r_text or "")
            fields = (((j or {}).get("error") or {}).get("fields") or [])
            if isinstance(fields, list) and fields:
                code = fields[0].get("code")
                return int(code) if code is not None else None
        except Exception:
            return None
        return None


    def tentar_atualizar_nfe_safe(self, nfe_id: int, payload: dict) -> bool:
        """
        Tenta atualizar a NF-e. Se o Bling disser que não permite edição (code 24),
        não explode: retorna False (skip update).
        """
        nfe_id = int(nfe_id)

        # tenta preservar numero/serie pra não cair no outro erro
        det = self.obter_nfe_detalhe(nfe_id)
        numero = det.get("numero") or det.get("numeroNota") or det.get("numero_nota")
        serie = det.get("serie") or det.get("serieNota") or det.get("serie_nota")

        payload2 = dict(payload or {})
        if numero:
            payload2["numero"] = int(str(numero).strip())
        if serie:
            payload2["serie"] = str(serie).strip()

        r = self._put(f"/nfe/{nfe_id}", json=payload2, timeout=(10, 90))
        if r.status_code in (200, 201):
            return True

        code = self._bling_error_code(r.text or "")

        # code 24 = “não permite edição”
        if code == 24:
            return False

        raise Exception(f"❌ Bling atualizar NF-e falhou: {r.status_code} - {r.text}")

    def baixar_danfe_simplificado_por_nfe(self, nfe_id: int) -> bytes:
        det = self._get_json(f"/nfe/{int(nfe_id)}")
        obj = det.get("data") or det

        keys_pref = {
            "linkdanfesimplificadoetiqueta",
            "linkdanfeetiqueta",
            "linkimpressaodanfeetiqueta",
            "linkdanfesimplificado",
            "linkdanfesimplificada",
            "linkdanfesimpl",
            "linkimpressaodanfe",
            "linkdanfe",
            "linkpdf",
            "linkpdfdanfe",
            "linkdanfepdf",
        }

        link = self._find_first_key_like(obj, keys_pref)
        if not link:
            link = self._find_first_url_containing(obj, "simpl")
        if not link:
            link = self._find_first_url_containing(obj, "danfe")
        if not link:
            link = self._find_first_url_containing(obj, ".pdf")

        if not link:
            raise Exception("NF-e não retornou link de DANFE/PDF (nem simplificado).")

        content = self._download(str(link).strip())
        if not content:
            raise Exception("Download do DANFE retornou vazio.")
        return content

    def baixar_danfe_por_pedido_venda(self, bling_pv_id: int, numero_loja: str | None = None) -> tuple[bytes, str]:
        pv_resp = self._get_json(f"/pedidos/vendas/{int(bling_pv_id)}")
        pv = pv_resp.get("data") or pv_resp

        link = None
        if isinstance(pv, dict):
            candidates = [
                pv.get("linkDanfe"),
                pv.get("linkDANFE"),
                pv.get("linkDanfePdf"),
                pv.get("linkPdfDanfe"),
            ]
            candidates = [c for c in candidates if isinstance(c, str) and c.strip()]
            for c in candidates:
                if "simpl" in c.lower():
                    link = c.strip()
                    break
            if not link and candidates:
                link = candidates[0].strip()

        if not link:
            link = self._find_first_url_containing(pv, "simpl")
        if not link:
            link = self._find_first_url_containing(pv, "danfe")

        if link:
            content = self._download(link)
            if _is_pdf(content):
                return content, "PDF"
            if _is_html(content):
                return content, "HTML"
            return content, "HTML"

        nfe_id = self.encontrar_nfe_id_por_pedido_venda(int(bling_pv_id), numero_loja=numero_loja)
        if not nfe_id:
            raise Exception("Bling não retornou link de DANFE no pedido/venda e não encontrei NF-e relacionada.")

        content = self.baixar_danfe_simplificado_por_nfe(int(nfe_id))
        if _is_pdf(content):
            return content, "PDF"
        if _is_html(content):
            return content, "HTML"
        return content, "HTML"

    def montar_payload_nfe_do_pv(self, pv_id: int) -> dict:
        pv_id = int(pv_id)
        pv_resp = self._get_json(f"/pedidos/vendas/{pv_id}")
        pv = pv_resp.get("data") or pv_resp
        if not isinstance(pv, dict):
            raise Exception("PV inválido")

        natureza_id = (os.getenv("BLING_NFE_NATUREZA_ID") or "").strip()
        if not natureza_id:
            raise Exception("Defina BLING_NFE_NATUREZA_ID no .env")

        contato = pv.get("contato") or {}
        numero_documento = (contato.get("numeroDocumento") or "").strip()
        if not numero_documento:
            raise Exception("PV sem contato.numeroDocumento")

        nome = (contato.get("nome") or "").strip()
        tipo = (contato.get("tipoPessoa") or "F").strip()

        end = contato.get("endereco") or {}
        endereco_nf = {
            "cep": (end.get("cep") or "").strip(),
            "uf": (end.get("uf") or "").strip(),
            "municipio": (end.get("municipio") or end.get("cidade") or "").strip(),
            "bairro": (end.get("bairro") or "").strip(),
            "endereco": (end.get("endereco") or end.get("logradouro") or "").strip(),
            "numero": (str(end.get("numero") or "").strip()),
            "complemento": (end.get("complemento") or "").strip(),
        }

        data_op = (pv.get("data") or pv.get("dataVenda") or "").strip()
        if isinstance(data_op, str) and "T" in data_op:
            data_op = data_op.split("T")[0]
        if not data_op:
            data_op = datetime.now().strftime("%Y-%m-%d")

        itens_pv = pv.get("itens") or []
        if not itens_pv:
            raise Exception("PV sem itens")

        itens_nf = []
        for it in itens_pv:
            obj = it.get("item") if isinstance(it, dict) and isinstance(it.get("item"), dict) else it
            if not isinstance(obj, dict):
                continue

            codigo = (obj.get("codigo") or obj.get("codigoProduto") or obj.get("sku") or "").strip()
            if not codigo:
                continue

            qtd = obj.get("quantidade") or 1
            valor = obj.get("valor") or 0

            try:
                qtd = float(str(qtd).replace(",", "."))
            except Exception:
                qtd = 1.0
            try:
                valor = float(str(valor).replace(",", "."))
            except Exception:
                valor = 0.0

            itens_nf.append({"codigo": codigo, "quantidade": qtd, "valor": valor})

        payload = {
            "idNaturezaOperacao": int(natureza_id),
            "dataOperacao": data_op,
            "contato": {
                "nome": nome,
                "numeroDocumento": numero_documento,
                "tipoPessoa": tipo,
                "endereco": endereco_nf,
            },
            "itens": itens_nf,
            "transporte": {"modalidadeFrete": 1},  # FOB
        }
        return payload

    def atualizar_nfe(self, nfe_id: int, payload: dict) -> bool:
        nfe_id = int(nfe_id)
        r = self._put(f"/nfe/{nfe_id}", json=payload, timeout=(10, 90))
        if r.status_code not in (200, 201):
            raise Exception(f"❌ Bling atualizar NF-e falhou: {r.status_code} - {r.text}")
        return True

    # ------------------------
    # ETIQUETAS (Bling)
    # ------------------------
    def _etiquetas_request_variations(self, formato: str, ids_vendas: list[int]):
        formato = (formato or "ZPL").upper()
        ids_vendas = [int(x) for x in (ids_vendas or []) if _safe_int(x) is not None]
        if not ids_vendas:
            raise Exception("ids_vendas vazio (não dá pra pedir etiqueta).")

        url = f"{self.base_url}/logisticas/etiquetas"

        paramsA = [("formato", formato)] + [("idsVendas[]", str(x)) for x in ids_vendas]
        yield ("GET params idsVendas[]", requests.get(url, headers=self._headers(), params=paramsA, timeout=(10, 90)))

        paramsB = {"formato": formato, "idsVendas": ",".join(str(x) for x in ids_vendas)}
        yield ("GET params idsVendas csv", requests.get(url, headers=self._headers(), params=paramsB, timeout=(10, 90)))

        paramsC = {"formato": formato}
        bodyC = {"idsVendas": ids_vendas}
        yield (
            "GET json body idsVendas",
            requests.request("GET", url, headers=self._headers(), params=paramsC, json=bodyC, timeout=(10, 90)),
        )

        paramsD = {"formato": formato}
        bodyD = {"idsVendas": ids_vendas}
        yield ("POST json body idsVendas", requests.post(url, headers=self._headers(), params=paramsD, json=bodyD, timeout=(10, 90)))

    def _parse_etiquetas_response(self, r) -> tuple[str | None, str | None]:
        try:
            data = r.json() or {}
        except Exception:
            return (None, None)

        itens = data.get("data") or []
        if not itens:
            return (None, None)

        for it in itens:
            if not isinstance(it, dict):
                continue
            link = it.get("link") or it.get("url") or it.get("download") or it.get("linkDownload")
            conteudo = it.get("conteudo") or it.get("content") or it.get("base64") or it.get("pdfBase64") or it.get("zpl")
            if link or conteudo:
                return (str(link).strip() if link else None, str(conteudo) if conteudo else None)
        return (None, None)

    def obter_etiqueta_link_ou_conteudo(self, ids_vendas: list[int], formato: str) -> tuple[str | None, str | None]:
        last_err = None
        for label, r in self._etiquetas_request_variations(formato=formato, ids_vendas=ids_vendas):
            if r.status_code != 200:
                last_err = f"{label}: {r.status_code} - {r.text}"
                continue
            link, conteudo = self._parse_etiquetas_response(r)
            if link or conteudo:
                return (link, conteudo)
        raise Exception(f"❌ Bling etiquetas falhou: {last_err or 'sem resposta útil'}")

    def obter_etiqueta_zpl(self, ids_vendas: list[int]) -> str:
        link, conteudo = self.obter_etiqueta_link_ou_conteudo(ids_vendas=ids_vendas, formato="ZPL")
        if conteudo and "^XA" in conteudo:
            return conteudo.strip()

        if link:
            raw = self._download(link)
            txt = raw.decode("utf-8", errors="ignore").strip()
            if txt and "^XA" not in txt:
                try:
                    dec = base64.b64decode(txt, validate=False).decode("utf-8", errors="ignore")
                    if "^XA" in dec:
                        return dec.strip()
                except Exception:
                    pass
            return txt
        return ""

    def obter_etiqueta_pdf(self, ids_vendas: list[int]) -> bytes:
        link, conteudo = self.obter_etiqueta_link_ou_conteudo(ids_vendas=ids_vendas, formato="PDF")
        if conteudo:
            try:
                b = base64.b64decode(str(conteudo), validate=False)
                if b:
                    return b
            except Exception:
                pass
        if link:
            return self._download(link)
        return b""

    # ------------------------
    # NF-e: CRIAR + ENVIAR + AGUARDAR AUTORIZAÇÃO
    # ------------------------

    def debug_campos_fiscais_nfe(self, nfe_id: int):
        """
        Mostra exatamente quais campos fiscais estão vazios
        antes do envio pra SEFAZ.
        """

        nf = self._get_json(f"/nfe/{int(nfe_id)}")
        d = nf.get("data") or nf

        print("\n================ DEBUG FISCAL NF-e ================")

        # ---------------- DESTINATÁRIO ----------------
        dest = d.get("contato") or d.get("destinatario") or {}
        print("\n--- DESTINATÁRIO ---")
        print("nome:", dest.get("nome"))
        print("doc:", dest.get("numeroDocumento") or dest.get("cpf") or dest.get("cnpj"))

        end = dest.get("endereco") or {}
        print("cep:", end.get("cep"))
        print("uf:", end.get("uf"))
        print("cidade:", end.get("municipio") or end.get("cidade"))
        print("bairro:", end.get("bairro"))
        print("logradouro:", end.get("endereco"))
        print("numero:", end.get("numero"))

        # ---------------- NATUREZA ----------------
        print("\n--- NATUREZA ---")
        print("naturezaOperacao:", d.get("naturezaOperacao"))
        print("idNaturezaOperacao:", d.get("idNaturezaOperacao"))

        # ---------------- TRANSPORTE ----------------
        transp = d.get("transporte") or {}
        print("\n--- TRANSPORTE ---")
        print("modalidadeFrete:", transp.get("modalidadeFrete"))

        # ---------------- ITENS ----------------
        print("\n--- ITENS ---")
        itens = d.get("itens") or []
        print("quantidade_itens:", len(itens))

        for i, it in enumerate(itens, 1):
            prod = it.get("produto") or {}
            trib = it.get("tributacao") or {}

            print(f"\nITEM {i}")
            print("codigo:", prod.get("codigo") or it.get("codigo"))
            print("descricao:", prod.get("descricao") or it.get("descricao"))
            print("ncm:", prod.get("ncm") or it.get("ncm"))
            print("cfop:", it.get("cfop"))
            print("origem:", trib.get("origem"))
            print("cst:", trib.get("cst"))
            print("csosn:", trib.get("csosn"))

        print("\n===================================================\n")

    def criar_nfe_por_pv(self, pv_id: int) -> int:
        pv_id = int(pv_id)
        pv_resp = self._get_json(f"/pedidos/vendas/{pv_id}")
        pv = pv_resp.get("data") or pv_resp
        if not isinstance(pv, dict):
            raise Exception("PV inválido")

        # -------------------------
        # 1) Natureza (obrigatório em muitas contas)
        # -------------------------
        natureza_id = None
        # tenta achar no PV em vários formatos
        nat = pv.get("naturezaOperacao") or pv.get("natureza") or {}
        if isinstance(nat, dict):
            natureza_id = nat.get("id") or nat.get("idNaturezaOperacao")

        if not natureza_id:
            natureza_id = (os.getenv("BLING_NFE_NATUREZA_ID") or "").strip() or None

        if not natureza_id:
            raise Exception("Sem idNaturezaOperacao. Defina BLING_NFE_NATUREZA_ID no .env")

        # -------------------------
        # 2) Destinatário/contato completo (muito comum ser obrigatório)
        # -------------------------
        contato = pv.get("contato") or {}
        numero_documento = (contato.get("numeroDocumento") or "").strip()
        if not numero_documento:
            raise Exception("PV sem contato.numeroDocumento (CPF/CNPJ)")

        nome = (contato.get("nome") or "").strip()
        tipo = (contato.get("tipoPessoa") or "F").strip()

        # Endereço (no seu PV tem tudo)
        end = contato.get("endereco") or pv.get("enderecoEntrega") or pv.get("endereco") or {}
        # tenta também variações
        endereco_nf = {
            "cep": (end.get("cep") or "").strip(),
            "uf": (end.get("uf") or "").strip(),
            "municipio": (end.get("municipio") or end.get("cidade") or "").strip(),
            "bairro": (end.get("bairro") or "").strip(),
            "endereco": (end.get("endereco") or end.get("logradouro") or "").strip(),
            "numero": (str(end.get("numero") or "").strip()),
            "complemento": (end.get("complemento") or "").strip(),
        }

        # -------------------------
        # 3) Datas
        # -------------------------
        data_op = (pv.get("data") or pv.get("dataVenda") or pv.get("dataEmissao") or "").strip()
        if isinstance(data_op, str) and "T" in data_op:
            data_op = data_op.split("T")[0]
        if not data_op:
            data_op = datetime.now().strftime("%Y-%m-%d")

        # -------------------------
        # 4) Itens (tenta mandar o máximo possível)
        # -------------------------
        itens_pv = pv.get("itens") or []
        if not itens_pv:
            raise Exception("PV sem itens")

        itens_nf = []
        for it in itens_pv:
            obj = it.get("item") if isinstance(it, dict) and isinstance(it.get("item"), dict) else it
            if not isinstance(obj, dict):
                continue

            codigo = (obj.get("codigo") or obj.get("codigoProduto") or obj.get("sku") or "").strip()
            if not codigo:
                continue

            qtd = obj.get("quantidade") or obj.get("qtde") or obj.get("qtd") or 1
            valor = obj.get("valor") or obj.get("preco") or obj.get("valorUnitario") or obj.get("valor_unitario") or 0

            try:
                qtd = float(str(qtd).replace(",", "."))
            except Exception:
                qtd = 1.0

            try:
                valor = float(str(valor).replace(",", "."))
            except Exception:
                valor = 0.0

            # Se sua conta exigir NCM/CFOP/CSOSN, o ideal é buscar do cadastro do produto.
            # Aqui vai “mínimo + deixa Bling calcular via natureza” (se ele conseguir).
            item_nf = {
                "codigo": codigo,
                "quantidade": qtd,
                "valor": valor,
            }

            itens_nf.append(item_nf)

        if not itens_nf:
            raise Exception("Não consegui montar itens NF-e")

        # -------------------------
        # 5) Frete / modalidade (no seu PV é FOB)
        # -------------------------
        # Bling costuma usar modalidadeFrete = 1 (FOB) ou equivalente.
        # Se sua conta não exigir, ele ignora; se exigir, ajuda.
        frete_mode = 1  # FOB

        # -------------------------
        # 6) Payload final (mais completo)
        # -------------------------
        payload = {
            "idPedidoVenda": pv_id,
            "idNaturezaOperacao": int(natureza_id),

            "dataOperacao": data_op,

            "contato": {
                "nome": nome,
                "numeroDocumento": numero_documento,
                "tipoPessoa": tipo,
                "endereco": endereco_nf,
            },

            "itens": itens_nf,

            # ajuda em contas que travam por frete
            "transporte": {
                "modalidadeFrete": frete_mode
            },
        }

        r = self._post("/nfe", json=payload, timeout=(10, 90))
        if r.status_code not in (200, 201):
            raise Exception(f"❌ Bling criar NF-e falhou: {r.status_code} - {r.text}")

        data = r.json() or {}
        obj = data.get("data") or data
        nfe_id = _safe_int((obj or {}).get("id")) or _safe_int((obj or {}).get("idNotaFiscal"))
        if not nfe_id:
            nfe_id = _find_first_int_key_like(data, {"id"})
        if not nfe_id:
            raise Exception(f"❌ Bling não retornou id da NF-e ao criar. Resp: {data}")

        return int(nfe_id)

    def enviar_nfe(self, nfe_id: int, enviar_email: bool = False):
        nfe_id = int(nfe_id)
        params = {"enviarEmail": "true" if enviar_email else "false"}
        r = self._post(f"/nfe/{nfe_id}/enviar", params=params, timeout=(10, 120))
        if r.status_code not in (200, 201, 202):
            raise Exception(f"❌ Bling enviar NF-e falhou: {r.status_code} - {r.text}")
        return True

    def obter_nfe(self, nfe_id: int) -> dict:
        return self._get_json(f"/nfe/{int(nfe_id)}", timeout=(10, 90))

    def aguardar_nfe_autorizada(self, nfe_id: int, timeout_s: int = 120, poll_s: float = 2.0) -> dict:
        nfe_id = int(nfe_id)
        t0 = time.time()
        last = None

        while time.time() - t0 < timeout_s:
            last = self.obter_nfe(nfe_id)
            obj = last.get("data") or last

            situ = obj.get("situacao") if isinstance(obj, dict) else None
            if situ is None and isinstance(obj, dict):
                situ = obj.get("status")

            if str(situ) == "6":
                return last

            try:
                dump = json.dumps(obj, ensure_ascii=False).lower()
                if "rejeit" in dump or "cancel" in dump:
                    raise Exception(f"NF-e não autorizou (status/situação indica rejeição/cancelamento). Resp={obj}")
            except Exception:
                pass

            time.sleep(poll_s)

        raise Exception(f"❌ Timeout aguardando NF-e autorizar. nfe_id={nfe_id} last={last}")

    def gerar_rascunho_nfe_por_pv(self, pv_id: int):
        """
        Tenta criar NF-e (rascunho) a partir do PV.
        Retorna (nfe_id, "OK") ou (None, "ERRO: ...").
        """
        pv_id = int(pv_id)
        payload = {"idPedidoVenda": pv_id}

        r = self._post("/nfe", json=payload, timeout=(10, 90))

        # Log útil
        try:
            body_txt = (r.text or "")[:1000]
        except Exception:
            body_txt = "<sem texto>"

        if r.status_code not in (200, 201):
            return None, f"HTTP_{r.status_code}: {body_txt}"

        try:
            data = r.json() or {}
        except Exception:
            return None, f"JSON_INVALIDO: {body_txt}"

        obj = data.get("data") or data

        # às vezes vem lista
        if isinstance(obj, list):
            obj = obj[0] if obj else {}

        nfe_id = (obj or {}).get("id") or (obj or {}).get("idNotaFiscal")
        if not nfe_id:
            return None, f"NAO_CRIADA: {data}"

        return int(nfe_id), "OK"

    def emitir_nfe_do_pv(self, pv_id: int, timeout_s: int = 120) -> int:
        pv_id = int(pv_id)
        nfe_id = self.criar_nfe_por_pv(pv_id)
        self.enviar_nfe(nfe_id, enviar_email=False)
        self.aguardar_nfe_autorizada(nfe_id, timeout_s=timeout_s, poll_s=2.0)
        return int(nfe_id)

    def gerar_rascunho_nfe_do_pv(self, pv_id: int):
        """
        Converte Pedido de Venda em NF-e rascunho válida para SEFAZ
        """

        pv = self.get_pedido_venda_detalhe(pv_id)
        d = pv.get("data") or pv

        contato = d.get("contato") or {}
        itens = d.get("itens") or []

        if not itens:
            return None, "PV_SEM_ITENS"

        numero_doc = (contato.get("numeroDocumento") or "").strip()
        if not numero_doc:
            return None, "CLIENTE_SEM_DOCUMENTO"

        payload = {
            "naturezaOperacao": "Venda de mercadoria",
            "dataOperacao": d.get("data") or d.get("dataVenda"),
            "contato": {
                "nome": contato.get("nome"),
                "numeroDocumento": numero_doc,
                "tipoPessoa": contato.get("tipoPessoa", "F"),
            },
            "itens": []
        }

        for it in itens:
            item = it.get("item") or it

            payload["itens"].append({
                "descricao": item.get("descricao"),
                "quantidade": item.get("quantidade"),
                "valor": item.get("valor") or item.get("preco"),
                "codigo": item.get("codigo"),
            })

        r = self._post("/nfe", json=payload)

        if r.status_code not in (200, 201):
            return None, f"HTTP_{r.status_code}: {r.text}"

        data = r.json()
        nfe_id = (data.get("data") or {}).get("id")

        return nfe_id, "RASCUNHO_CRIADO"

    def enviar_nfe_para_sefaz(self, nfe_id: int):
        """
        Envia a NF-e rascunho para autorização (SEFAZ) via endpoint do Bling.
        """
        nfe_id = int(nfe_id)
        r = self._post(f"/nfe/{nfe_id}/enviar", timeout=(10, 120))

        if r.status_code not in (200, 201, 202):
            return False, (r.text or "")

        return True, "ENVIADA"

    def get_nf(nf_id: int, token: str):
        r = requests.get(
            f"{BASE}/notas/fiscais/{nf_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def dump_nf_check(nf: dict):
        d = nf.get("data") or nf
        print("situacao=", d.get("situacao"))
        print("naturezaOperacao=", d.get("naturezaOperacao") or d.get("idNaturezaOperacao"))
        dest = d.get("contato") or d.get("destinatario") or {}
        print("dest.nome=", dest.get("nome"))
        print("dest.doc=", dest.get("numeroDocumento") or dest.get("cpf") or dest.get("cnpj"))
        end = dest.get("endereco") or {}
        print("dest.end=", end.get("endereco"), end.get("numero"), end.get("bairro"), end.get("municipio"), end.get("uf"), end.get("cep"))

        itens = d.get("itens") or []
        print("itens=", len(itens))
        for i, it in enumerate(itens, 1):
            prod = it.get("produto") or {}
            trib = it.get("tributacao") or {}
            print(f"[{i}] sku/cod=", prod.get("codigo") or prod.get("sku"))
            print(f"[{i}] ncm=", prod.get("ncm") or it.get("ncm"))
            print(f"[{i}] cfop=", it.get("cfop"))
            print(f"[{i}] origem=", trib.get("origem"))
            print(f"[{i}] cst/csosn=", trib.get("cst") or trib.get("csosn"))

    # uso:
    # nf = get_nf(25150583317, token=os.getenv("BLING_ACCESS_TOKEN"))
    # dump_nf_check(nf)

    def atualizar_situacao_pv(self, pv_id: int, situacao_id: int) -> bool:
        pv_id = int(pv_id)
        situacao_id = int(situacao_id)

        # pega PV pra montar payload completo (PUT exige várias coisas)
        resp = self._get_json(f"/pedidos/vendas/{pv_id}")
        pv = resp.get("data") or resp
        if not isinstance(pv, dict):
            raise Exception("PV inválido")

        contato = pv.get("contato") or {}
        contato_id = contato.get("id")
        if not contato_id:
            raise Exception("PV sem contato.id")

        itens_in = pv.get("itens") or []
        if not itens_in:
            raise Exception("PV sem itens")

        # normaliza itens
        itens_out = []
        for it in itens_in:
            item = it.get("item") if isinstance(it, dict) else it
            if not isinstance(item, dict):
                continue

            codigo = item.get("codigo") or item.get("sku") or (item.get("produto") or {}).get("codigo")
            qtd = item.get("quantidade") or 1
            val = item.get("valor") or item.get("preco") or 0

            if not codigo:
                continue

            itens_out.append({
                "quantidade": float(qtd),
                "valor": float(val),
                "produto": {"codigo": str(codigo)},
            })

        body = {
            "id": pv_id,
            "contato": {"id": int(contato_id)},
            "itens": itens_out,
            "situacao": {"id": situacao_id},
        }

        # mantém numeroLoja se existir (ajuda)
        numero_loja = pv.get("numeroLoja")
        if numero_loja:
            body["numeroLoja"] = str(numero_loja)

        r = self._put(f"/pedidos/vendas/{pv_id}", json=body, timeout=(10, 90))
        if r.status_code not in (200, 201):
            raise Exception(f"❌ Falhou setar situação: {r.status_code} - {r.text}")

        return True

    def esperar_nfe_autorizada(self, nfe_id: int, timeout: int = 120, poll_s: float = 2.0):
        """
        Aguarda até a NF-e ficar AUTORIZADA.
        Usa o seu helper nfe_esta_autorizada(), que já trata string/código.
        """
        nfe_id = int(nfe_id)
        t0 = time.time()
        last = None

        while time.time() - t0 < timeout:
            try:
                last = self.obter_nfe_detalhe(nfe_id)  # retorna dict "data" já normalizado
            except Exception:
                last = None

            if isinstance(last, dict):
                raw = last.get("situacao") if last.get("situacao") is not None else last.get("status")
                print("status:", raw, flush=True)

            try:
                if self.nfe_esta_autorizada(nfe_id):
                    return True, "AUTORIZADA"
            except Exception:
                pass

            time.sleep(poll_s)

        return False, "TIMEOUT"


    def baixar_danfe_simplificado(self, nfe_id: int) -> bytes | None:
        """
        Baixa o DANFE (preferindo simplificado) usando o método robusto por links.
        """
        try:
            return self.baixar_danfe_simplificado_por_nfe(int(nfe_id))
        except Exception:
            return None


    # ------------------------
    # Situações / Módulos (pra filtrar "Em aberto")
    # ------------------------
    def listar_modulos_situacoes(self) -> list[dict]:
        data = self._get_json("/situacoes/modulos")
        itens = data.get("data") or []
        return [x for x in itens if isinstance(x, dict)]

    def listar_situacoes_do_modulo(self, id_modulo: int) -> list[dict]:
        data = self._get_json(f"/situacoes/modulos/{int(id_modulo)}")
        itens = data.get("data") or []
        return [x for x in itens if isinstance(x, dict)]

    def obter_modulo_id_pedidos_vendas(self) -> int | None:
        mods = self.listar_modulos_situacoes()
        for m in mods:
            nome = str(m.get("nome") or m.get("descricao") or "").strip().lower()
            # tenta bater por texto (depende da conta/idioma)
            if "pedido" in nome and "venda" in nome:
                mid = _safe_int(m.get("id"))
                if mid:
                    return int(mid)
        return None

    def obter_situacao_id_por_nome(self, id_modulo: int, nome_alvo: str) -> int | None:
        nome_alvo = (nome_alvo or "").strip().lower()
        itens = self.listar_situacoes_do_modulo(id_modulo)
        for s in itens:
            nome = str(s.get("nome") or s.get("descricao") or "").strip().lower()
            if nome == nome_alvo:
                sid = _safe_int(s.get("id"))
                if sid:
                    return int(sid)
        return None

    def obter_situacao_id_em_aberto(self) -> int | None:
        mid = self.obter_modulo_id_pedidos_vendas()
        if not mid:
            return None
        return self.obter_situacao_id_por_nome(mid, "em aberto")

    def _pv_get_data(self, pv_id: int) -> dict:
        resp = self._get_json(f"/pedidos/vendas/{int(pv_id)}")
        return resp.get("data") or resp or {}

    def _pv_extract_obs(self, pv_data: dict) -> str:
        # Bling varia nomes; tentamos vários
        for k in ("observacoes", "observacao", "obs", "observations", "observacaoInterna", "observacoesInternas"):
            v = pv_data.get(k)
            if isinstance(v, str):
                return v
        return ""

    def _pv_set_obs_payload(self, pv_data: dict, new_obs: str) -> dict:
        """
        Monta payload mínimo possível.
        Tentamos PATCH com observacoes; se falhar, tentamos PUT com um objeto mais completo.
        """
        # PATCH mínimo
        return {"observacoes": new_obs}



# dentro da class BlingService:

    def atualizar_observacoes_pv(self, pv_id: int, observacoes: str) -> bool:
        pv_id = int(pv_id)

        # 1) Lê PV
        resp = self._get_json(f"/pedidos/vendas/{pv_id}")
        pv = resp.get("data") or resp
        if not isinstance(pv, dict):
            raise Exception("PV inválido (não veio dict)")

        # 2) Pega contato.id (obrigatório)
        contato = pv.get("contato") or {}
        contato_id = contato.get("id") or (contato.get("data") or {}).get("id")
        if not contato_id:
            raise Exception("PV sem contato.id")

        # 3) Data (normaliza pra YYYY-MM-DD)
        data_pv = (
            pv.get("data") or pv.get("dataEmissao") or pv.get("dataVenda") or pv.get("dataCriacao")
        )
        if isinstance(data_pv, str) and "T" in data_pv:
            data_pv = data_pv.split("T")[0]
        if not data_pv:
            data_pv = datetime.now().strftime("%Y-%m-%d")

        # 4) Itens (obrigatório)
        itens_in = pv.get("itens") or []
        if not isinstance(itens_in, list) or not itens_in:
            raise Exception("PV sem itens")

        itens_out = []
        for it in itens_in:
            if not isinstance(it, dict):
                continue

            item = it.get("item") if isinstance(it.get("item"), dict) else it
            prod = item.get("produto") if isinstance(item.get("produto"), dict) else {}

            prod_id = prod.get("id") or item.get("idProduto") or item.get("produtoId")
            codigo = prod.get("codigo") or item.get("codigo") or item.get("sku")

            if not prod_id and not codigo:
                continue

            qtd = item.get("quantidade") or 1
            val = item.get("valor") or item.get("preco") or item.get("valorUnitario") or 0

            payload_item = {
                "quantidade": float(qtd),
                "valor": float(val),
                "produto": {"id": int(prod_id)} if prod_id else {"codigo": str(codigo)},
            }
            itens_out.append(payload_item)

        if not itens_out:
            raise Exception("Não consegui montar itens válidos para PUT")

        # 5) Payload NO ROOT (sem {"data":{...}})
        body = {
            "id": pv_id,
            "data": str(data_pv),
            "contato": {"id": int(contato_id)},
            "itens": itens_out,
            "observacoes": str(observacoes or "").strip(),
        }

        # Mantém numeroLoja se existir (às vezes ajuda)
        numero_loja = pv.get("numeroLoja") or pv.get("numero_loja")
        if numero_loja:
            body["numeroLoja"] = str(numero_loja)

        r = self._put(f"/pedidos/vendas/{pv_id}", json=body, timeout=(10, 90))
        if r.status_code not in (200, 201):
            raise Exception(f"❌ PUT obs falhou: {r.status_code} - {r.text}")

        return True


    def _formatar_seriais_para_obs(self, ml_id: str, serials_by_sku: dict) -> str:
        """
        Monta um bloco padronizado pra colar nas observações.
        """
        lines = []
        lines.append("=== SERIAIS (WMS) ===")
        if ml_id:
            lines.append(f"ML_ID: {ml_id}")

        for sku, serials in (serials_by_sku or {}).items():
            sku = str(sku or "").strip()
            serials = serials or []
            serials_txt = ", ".join(str(x).strip() for x in serials if str(x).strip())
            if sku and serials_txt:
                lines.append(f"{sku}: {serials_txt}")

        lines.append("=====================")
        return "\n".join(lines).strip()


    def anexar_seriais_em_observacoes(self, pv_id: int, ml_id: str, serials_by_sku: dict) -> bool:
        """
        Busca o PV atual, anexa um bloco de seriais no fim das observações
        e atualiza no Bling.
        """
        pv_id = int(pv_id)
        ml_id = str(ml_id or "").strip()

        pv = self.get_pedido_venda_detalhe(pv_id)
        data = pv.get("data") if isinstance(pv, dict) and "data" in pv else pv
        if not isinstance(data, dict):
            raise Exception("PV detalhe veio num formato inesperado.")

        obs_atual = (data.get("observacoes") or data.get("observacao") or "").strip()
        bloco = self._formatar_seriais_para_obs(ml_id, serials_by_sku)

        # evita duplicar se rodar 2x
        if bloco in obs_atual:
            return True

        new_obs = (obs_atual + "\n\n" + bloco).strip() if obs_atual else bloco
        return self.atualizar_observacoes_pv(pv_id, new_obs)


    def pedido_venda_exige_serial(self, pv_id: int) -> bool:
        """
        Regra simples (do seu .env):
        - SERIAL_REQUIRED_SKUS=SKU123,SKU456
        - SERIAL_REQUIRED_PREFIXES=ANV-,HUM-
        Se qualquer item do PV bater, exige serial.
        """
        pv_id = int(pv_id)
        pv = self.get_pedido_venda_detalhe(pv_id)
        data = pv.get("data") if isinstance(pv, dict) and "data" in pv else pv
        if not isinstance(data, dict):
            return False

        skus_req = set(x.strip() for x in (os.getenv("SERIAL_REQUIRED_SKUS") or "").split(",") if x.strip())
        pref_req = [x.strip() for x in (os.getenv("SERIAL_REQUIRED_PREFIXES") or "").split(",") if x.strip()]

        itens = data.get("itens") or []
        for it in itens if isinstance(itens, list) else []:
            item = it.get("item") if isinstance(it, dict) and isinstance(it.get("item"), dict) else it
            if not isinstance(item, dict):
                continue

            sku = (
                item.get("codigo") or item.get("sku") or item.get("codigoProduto") or item.get("codigo_produto")
                or ((item.get("produto") or {}).get("codigo") if isinstance(item.get("produto"), dict) else None)
            )
            sku = str(sku or "").strip()
            if not sku:
                continue

            if sku in skus_req:
                return True
            for p in pref_req:
                if sku.startswith(p):
                    return True

        return False