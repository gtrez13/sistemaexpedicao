import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv, set_key
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypdf import PdfReader, PdfWriter, Transformation
from io import BytesIO

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)

# Tipos de logística que você trata como Agência/Coleta
AGENCIA_TYPES = {"cross_docking", "drop_off", "xd_drop_off"}

# Liga logs chatos só se ML_DEBUG=1 no .env
ML_DEBUG = (os.getenv("ML_DEBUG") or "").strip() in ("1", "true", "True", "yes", "YES")


def _log(msg: str):
    if ML_DEBUG:
        print(msg, flush=True)


def _parse_dt(s: str | None):
    try:
        if not s:
            return None
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _day_bounds_local(offset_hours=-3, day: datetime | None = None):
    tz = timezone(timedelta(hours=offset_hours))
    now = day.astimezone(tz) if day else datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _norm_substatus(s: str | None) -> str:
    s = (s or "").strip().lower()
    if s == "ready_to_prrint":
        return "ready_to_print"
    if s == "invoice_pendding":
        return "invoice_pending"
    if s == "in_packing_llist":
        return "in_packing_list"
    return s


def _norm_logistic(s: str | None) -> str:
    s = (s or "").strip().lower()
    if s == "self_servicee":
        return "self_service"
    return s


def _best_ship_datetime(sh: dict, order: dict):
    candidates = [
        sh.get("shipping_limit_date"),
        sh.get("date_ready_to_ship"),
        (sh.get("status_history") or {}).get("date_ready_to_ship"),
        (sh.get("status_history") or {}).get("date_shipped"),
        sh.get("date_created"),
        sh.get("last_updated"),
        order.get("date_closed"),
        order.get("date_created"),
    ]
    for c in candidates:
        dt = _parse_dt(c)
        if dt:
            return dt
    return None


class MercadoLivreService:
    """
    FLEX: logistic_type == self_service AND shipment.substatus == ready_to_print
    AGÊNCIA: logistic_type in AGENCIA_TYPES AND (
        shipment.substatus == invoice_pending OR sem NF-e via /users/{id}/invoices/orders/{order_id}
    )
    Ignora fulfillment.
    """

    def __init__(self):
        self.base_url = "https://api.mercadolibre.com"
        self.client_id = os.getenv("ML_APP_ID")
        self.client_secret = os.getenv("ML_APP_SECRET")
        self.access_token = os.getenv("ML_ACCESS_TOKEN")
        self.refresh_token = os.getenv("ML_REFRESH_TOKEN")
        self.user_id = os.getenv("ML_SELLER_ID")

        if not all([self.client_id, self.client_secret, self.access_token, self.refresh_token, self.user_id]):
            print("⚠️ ML: Variáveis incompletas no .env", flush=True)

        self._shipment_cache: dict[str, dict | None] = {}
        self._invoice_cache: dict[str, bool] = {}
        self._items_cache: dict[str, dict] = {}  # item_id -> {sku,title, seller_custom_field, seller_sku}
        self._variation_cache: dict[tuple[str, str], str | None] = {}  # (item_id, variation_id) -> sku

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def obter_pedido(self, ml_order_id: str) -> dict:
        ml_order_id = str(ml_order_id).strip()
        url = f"https://api.mercadolibre.com/orders/{ml_order_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        print("🌐 ML GET", url, flush=True)
        print("🔑 token head =", repr((self.access_token or "")[:20]), flush=True)

        r = requests.get(url, headers=headers, timeout=30)
        print("📥 status =", r.status_code, flush=True)
        print("📥 body =", (r.text or "")[:300], flush=True)

        if r.status_code >= 400:
            raise Exception(f"ML obter_pedido falhou: {r.status_code} - {r.text}")

        return r.json()

    def baixar_etiqueta_pdf_4x6(self, shipment_id: str) -> bytes:
        """
        1) tenta /shipments/{id}/labels (quando existe, costuma vir melhor)
        2) se der 404 (ou não vier pdf), cai pro /shipment_labels (mais compatível)
        3) extrai apenas a 1ª página (descarta 2ª)
        4) gira +90° para deixar EM PÉ (portrait)
        """
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise Exception("shipment_id vazio")

        # Tentativa 1: endpoint "novo"
        r = self._request(
            "GET",
            f"/shipments/{shipment_id}/labels",
            params={"response_type": "pdf", "label_type": "shipping"},
        )

        pdf_bytes = None
        if r and r.status_code == 200:
            b = r.content or b""
            if b.startswith(b"%PDF"):
                pdf_bytes = b

        # Se endpoint não existir (404) ou não retornou PDF, fallback
        if pdf_bytes is None:
            if (r is None) or (r.status_code in (404, 400, 403)) or (r.status_code == 200):
                r2 = self._request(
                    "GET",
                    "/shipment_labels",
                    params={"shipment_ids": shipment_id, "response_type": "pdf"},
                )
                if not r2 or r2.status_code != 200:
                    raise Exception(f"Etiqueta ML fallback falhou: {getattr(r2,'status_code',None)} - {getattr(r2,'text','')}")
                b2 = r2.content or b""
                if not b2.startswith(b"%PDF"):
                    raise Exception(f"Etiqueta ML fallback não retornou PDF (bytes={len(b2)})")
                pdf_bytes = b2
            else:
                raise Exception(f"Etiqueta ML 4x6 falhou: {getattr(r,'status_code',None)} - {getattr(r,'text','')}")

        # Extrai 1ª página + gira +90° para deixar EM PÉ
        return self._extract_and_rotate_pdf(pdf_bytes)

    def _extract_and_rotate_pdf(self, pdf_bytes: bytes) -> bytes:
        from io import BytesIO
        from pypdf import PdfReader, PdfWriter, Transformation

        # =========================
        # CONFIG (mexe só aqui)
        # =========================
        MM_TO_PT = 72 / 25.4

        # Elgin L42-DT: largura máxima imprimível ~108mm (4")
        PAPER_W_MM = 108.0
        PAPER_H_MM = 152.4  # 6"

        dst_w = PAPER_W_MM * MM_TO_PT
        dst_h = PAPER_H_MM * MM_TO_PT

        ZOOM = 1.70          # começa 1.00, depois 1.05, 1.10, 1.15...
        MOVE_X_MM = - 1.80      # + direita | - esquerda
        MOVE_Y_MM = 0.0      # + sobe    | - desce

        # "FIT" = cabe tudo (evita virar faixa/ sumir)
        USE_FIT = True

        # Se a etiqueta estiver DEITADA de verdade, liga isso.
        ROTATE_90 = False

        # =========================
        # READ
        # =========================
        reader = PdfReader(BytesIO(pdf_bytes))
        if not reader.pages:
            raise Exception("Etiqueta ML: PDF sem páginas")

        src = reader.pages[0]

        # Usa cropbox (área útil) se tiver; senão mediabox
        box = getattr(src, "cropbox", None) or src.mediabox
        left = float(box.left)
        bottom = float(box.bottom)
        width = float(box.width)
        height = float(box.height)

        # =========================
        # 1) Normaliza: recorta conteúdo pra (0,0) usando a área útil
        # =========================
        tmp_writer = PdfWriter()
        content = tmp_writer.add_blank_page(width=width, height=height)

        content.merge_transformed_page(
            src,
            Transformation().translate(tx=-left, ty=-bottom)
        )

        # =========================
        # 2) Rotação opcional
        # =========================
        if ROTATE_90:
            content.rotate(90)

        # dimensões reais do conteúdo
        src_w = float(content.mediabox.width)
        src_h = float(content.mediabox.height)

        # =========================
        # 3) Scale + posição
        # =========================
        if USE_FIT:
            base_scale = min(dst_w / src_w, dst_h / src_h)  # FIT (não corta)
        else:
            base_scale = max(dst_w / src_w, dst_h / src_h)  # FILL (preenche, pode cortar)

        scale = base_scale * ZOOM

        new_w = src_w * scale
        new_h = src_h * scale

        # centraliza
        tx = (dst_w - new_w) / 2
        ty = (dst_h - new_h) / 2

        # ajuste manual em mm
        tx += MOVE_X_MM * MM_TO_PT
        ty += MOVE_Y_MM * MM_TO_PT

        # =========================
        # 4) Página final 4x6
        # =========================
        writer = PdfWriter()
        dst = writer.add_blank_page(width=dst_w, height=dst_h)

        dst.merge_transformed_page(
            content,
            Transformation().scale(scale).translate(tx=tx, ty=ty)
        )

        out = BytesIO()
        writer.write(out)
        return out.getvalue()

    def renovar_token(self):
        url = f"{self.base_url}/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        r = requests.post(url, data=payload, timeout=(10, 60))
        if r.status_code != 200:
            raise Exception(f"ML refresh falhou: {r.status_code} - {r.text}")

        data = r.json()
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        set_key(ENV_PATH, "ML_ACCESS_TOKEN", self.access_token)
        set_key(ENV_PATH, "ML_REFRESH_TOKEN", self.refresh_token)
        return True

    def _request(self, method, endpoint, params=None):
        url = f"{self.base_url}{endpoint}"
        for _ in range(3):
            try:
                r = requests.request(method, url, headers=self._headers(), params=params, timeout=(10, 60))
                if r.status_code == 401:
                    self.renovar_token()
                    continue
                return r
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                time.sleep(1)
        return None

    def _get_shipment(self, shipping_id: str):
        if shipping_id in self._shipment_cache:
            return self._shipment_cache[shipping_id]

        r = self._request("GET", f"/shipments/{shipping_id}")
        if not r or r.status_code != 200:
            self._shipment_cache[shipping_id] = None
            return None

        data = r.json()
        self._shipment_cache[shipping_id] = data
        return data

    def shipment_printable_status(self, shipment_id: str) -> tuple[bool, str, str | None, str | None]:
        """
        Retorna (printable, reason, status, substatus)
          - reason: OK | NEEDS_INVOICE | NOT_READY
        """
        sh = self._get_shipment(str(shipment_id))
        if not sh:
            return (False, "NOT_READY", None, None)

        status = (sh.get("status") or "").strip()
        sub = _norm_substatus(sh.get("substatus"))

        if sub == "invoice_pending":
            return (False, "NEEDS_INVOICE", status, sub)

        if status == "ready_to_ship" and sub in ("ready_to_print", "printed"):
            return (True, "OK", status, sub)

        return (False, "NOT_READY", status, sub)


    # ------------------------------------------------------------
    # Etiquetas (Mercado Envios) - PDF/ZPL
    # ------------------------------------------------------------
    def get_order(self, order_id: str) -> dict | None:
        """Busca um pedido no ML (para descobrir shipping.id quando não está no DB)."""
        order_id = str(order_id or "").strip()
        if not order_id:
            return None
        r = self._request("GET", f"/orders/{order_id}")
        if not r or r.status_code != 200:
            return None
        try:
            return r.json() or {}
        except Exception:
            return None

    def get_shipping_id_from_order(self, order_id: str) -> str | None:
        o = self.get_order(order_id)
        if not o:
            return None
        sid = (o.get("shipping") or {}).get("id")
        return str(sid) if sid else None

    def baixar_etiqueta_pdf(self, shipment_id: str) -> bytes:
        """
        Baixa etiqueta do Mercado Envios em PDF.
        Endpoint comum: /shipment_labels?shipment_ids={id}&response_type=pdf
        """
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise Exception("shipment_id vazio")
        r = self._request("GET", "/shipment_labels", params={"shipment_ids": shipment_id, "response_type": "pdf"})
        if not r or r.status_code != 200:
            raise Exception(f"Etiqueta ML falhou: {getattr(r,'status_code',None)} - {getattr(r,'text','')}")
        return r.content or b""

    def baixar_etiqueta_zpl(self, shipment_id: str) -> str:
        """
        Baixa etiqueta do Mercado Envios em ZPL (quando disponível).
        response_type costuma ser 'zpl2' (em algumas contas).
        """
        shipment_id = str(shipment_id or "").strip()
        if not shipment_id:
            raise Exception("shipment_id vazio")
        r = self._request("GET", "/shipment_labels", params={"shipment_ids": shipment_id, "response_type": "zpl2"})
        if not r or r.status_code != 200:
            raise Exception(f"Etiqueta ML ZPL falhou: {getattr(r,'status_code',None)} - {getattr(r,'text','')}")
        raw = r.content or b""
        if raw[:4] == b"%PDF":
            raise Exception("ML retornou PDF no response_type=zpl2 (conta/rota sem ZPL).")

        txt = (r.text or "").strip()
        if "^XA" in txt:
            return txt

        # algumas contas devolvem JSON (com base64 ou texto dentro)
        if txt.startswith("{") or txt.startswith("["):
            try:
                data = r.json()
                s = json.dumps(data)
                if "^XA" in s:
                    start = s.find("^XA")
                    end = s.find("^XZ", start)
                    if start >= 0 and end > start:
                        return s[start:end + 3]
            except Exception:
                pass

        return txt

    def _order_needs_invoice(self, order_id: str) -> bool:
        if order_id in self._invoice_cache:
            return self._invoice_cache[order_id]

        r = self._request("GET", f"/users/{self.user_id}/invoices/orders/{order_id}")
        if not r:
            self._invoice_cache[order_id] = False
            return False

        if r.status_code == 404:
            self._invoice_cache[order_id] = True
            return True

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                data = None

            if data in (None, [], {}):
                self._invoice_cache[order_id] = True
                return True
            if isinstance(data, list) and len(data) == 0:
                self._invoice_cache[order_id] = True
                return True

            self._invoice_cache[order_id] = False
            return False

        self._invoice_cache[order_id] = False
        return False

    def _fetch_orders_ready_to_ship(self, limit_total=400):
        pedidos = []
        offset = 0
        limit = 50

        while len(pedidos) < limit_total:
            params = {
                "seller": self.user_id,
                "order.status": "paid",
                "shipping.status": "ready_to_ship",
                "sort": "date_desc",
                "limit": limit,
                "offset": offset,
            }
            r = self._request("GET", "/orders/search", params=params)
            if not r or r.status_code != 200:
                raise Exception(f"orders/search falhou: {getattr(r,'status_code',None)} - {getattr(r,'text','')}")
            data = r.json() or {}
            results = data.get("results", []) or []
            if not results:
                break
            pedidos.extend(results)
            offset += limit

        return pedidos[:limit_total]

    def _extract_sku_from_attributes(self, attrs):
        if not isinstance(attrs, list):
            return None
        for a in attrs:
            if not isinstance(a, dict):
                continue
            aid = (a.get("id") or "").upper()
            name = (a.get("name") or "").upper()
            if aid in ("SELLER_SKU", "SELLER_CUSTOM_FIELD", "SKU") or "SKU" in name:
                val = a.get("value_name") or a.get("value_id") or a.get("value")
                if val:
                    return str(val).strip()
        return None

    def _fetch_items_bulk(self, item_ids: list[str]):
        missing = [i for i in item_ids if i and i not in self._items_cache]
        if not missing:
            return

        batch_size = 20
        for i in range(0, len(missing), batch_size):
            batch = missing[i:i + batch_size]
            ids = ",".join(batch)

            r = self._request("GET", "/items", params={"ids": ids})
            if not r or r.status_code != 200:
                continue

            arr = r.json() or []
            for entry in arr:
                body = entry.get("body") if isinstance(entry, dict) else None
                if not body or not isinstance(body, dict):
                    continue

                item_id = str(body.get("id"))
                title = body.get("title")

                custom = body.get("seller_custom_field")
                seller_sku = body.get("seller_sku")

                attr_sku = self._extract_sku_from_attributes(body.get("attributes")) \
                           or self._extract_sku_from_attributes(body.get("variation_attributes"))

                sku = custom or seller_sku or attr_sku

                self._items_cache[item_id] = {
                    "sku": (str(sku).strip() if sku else None),
                    "title": title,
                    "seller_custom_field": custom,
                    "seller_sku": seller_sku,
                }

    def _get_variation_sku(self, item_id: str, variation_id: str | int | None):
        if not item_id or not variation_id:
            return None

        key = (str(item_id), str(variation_id))
        if key in self._variation_cache:
            return self._variation_cache[key]

        r = self._request("GET", f"/items/{item_id}/variations/{variation_id}")
        if not r or r.status_code != 200:
            self._variation_cache[key] = None
            return None

        data = r.json() or {}

        sku = data.get("seller_custom_field") or data.get("seller_sku")
        if sku:
            sku = str(sku).strip()
            self._variation_cache[key] = sku
            return sku

        attrs = data.get("attributes") or []
        sku2 = self._extract_sku_from_attributes(attrs)
        self._variation_cache[key] = sku2
        return sku2

    # ✅ MÉTODO PRINCIPAL (ÚNICO)
    def buscar_cards(self, tz_offset_hours=-3):
        tz = timezone(timedelta(hours=tz_offset_hours))
        today = datetime.now(tz)

        days = [
            ("today", today),
            ("tomorrow", today + timedelta(days=1)),
            ("yesterday", today - timedelta(days=1)),
        ]
        day_ranges = {k: _day_bounds_local(tz_offset_hours, d) for k, d in days}

        orders = self._fetch_orders_ready_to_ship(limit_total=400)

        ship_ids = []
        order_by_ship = {}
        for o in orders:
            sid = (o.get("shipping") or {}).get("id")
            if sid:
                sid = str(sid)
                ship_ids.append(sid)
                order_by_ship[sid] = o

        def fetch_ship(sid_):
            return sid_, self._get_shipment(sid_)

        shipments = {}
        with ThreadPoolExecutor(max_workers=16) as ex:
            futures = [ex.submit(fetch_ship, sid) for sid in ship_ids]
            for fut in as_completed(futures):
                sid, sh = fut.result()
                shipments[sid] = sh

        flex, agencia = [], []

        for sid, order in order_by_ship.items():
            sh = shipments.get(sid)
            if not sh:
                _log(f"⚠️ SEM SHIPMENT: order={order.get('id')} sid={sid}")
                continue

            logistic_raw = sh.get("logistic_type") or ((sh.get("logistic") or {}).get("type")) or "Padrão"
            logistic_norm = _norm_logistic(logistic_raw)

            sub_raw = sh.get("substatus")
            substatus = _norm_substatus(sub_raw)
            ship_status = sh.get("status")

            if logistic_norm == "fulfillment":
                continue

            dt = _best_ship_datetime(sh, order)
            if dt:
                dt = dt.astimezone(tz)

            day_key = None
            if dt:
                for k, (start, end) in day_ranges.items():
                    if start <= dt <= end:
                        day_key = k
                        break

            if not day_key:
                day_key = "other"

            order["shipping"] = order.get("shipping") or {}
            order["shipping"]["ml_shipping_id"] = sid
            order["shipping"]["logistic_type"] = logistic_norm
            order["shipping"]["ml_ship_status"] = ship_status
            order["shipping"]["ml_ship_substatus"] = substatus
            order["ml_day_bucket"] = day_key

            if logistic_norm == "self_service" and substatus == "ready_to_print":
                order["ml_task"] = "PRINT_LABEL"
                flex.append(order)
                continue

            if logistic_norm in AGENCIA_TYPES:
                oid = str(order.get("id"))

                if substatus == "invoice_pending":
                    order["ml_task"] = "EMITIR_NFE"
                    agencia.append(order)
                    continue

                if self._order_needs_invoice(oid):
                    order["ml_task"] = "EMITIR_NFE"
                    agencia.append(order)
                    continue

        # resolve SKU via /items em lote
        all_item_ids = []
        for o in (flex + agencia):
            for oi in (o.get("order_items") or []):
                item = oi.get("item") or {}
                if item.get("id"):
                    all_item_ids.append(str(item.get("id")))

        uniq_item_ids = list(dict.fromkeys(all_item_ids))
        self._fetch_items_bulk(uniq_item_ids)

        # aplica SKU/Title nos itens do order
        for o in (flex + agencia):
            for oi in (o.get("order_items") or []):
                item = oi.get("item") or {}
                item_id = str(item.get("id")) if item.get("id") else None

                real = self._items_cache.get(item_id or "", {})
                real_sku = real.get("sku")
                real_title = real.get("title")

                var_id = oi.get("variation_id") or (oi.get("item") or {}).get("variation_id")
                var_sku = self._get_variation_sku(item_id, var_id) if (item_id and var_id) else None

                # ✅ prioridade: SKU da variação > SKU do item pai
                if var_sku:
                    oi["seller_sku"] = var_sku
                elif real_sku:
                    oi["seller_sku"] = real_sku
                else:
                    oi["seller_sku"] = oi.get("seller_sku") or (item_id or "SEM_SKU")

                    if var_sku:
                        oi["seller_sku"] = var_sku
                    else:
                        oi["seller_sku"] = oi.get("seller_sku") or (item_id or "SEM_SKU")

                if (not item.get("title")) and real_title:
                    item["title"] = real_title

        prio = {"today": 0, "tomorrow": 1, "yesterday": 2, "other": 3}

        def sort_key(o):
            dtx = _parse_dt(o.get("date_closed") or o.get("date_created") or "")
            dtx = (dtx.astimezone(tz) if dtx else datetime.min.replace(tzinfo=tz))
            return (prio.get(o.get("ml_day_bucket", "other"), 9), -dtx.timestamp())

        flex.sort(key=sort_key)
        agencia.sort(key=sort_key)

        return {
            "flex_ready_to_print": flex,
            "coleta_invoices_to_be_managed": agencia,
        }

    def resolver_ml_para_numero_loja(self, any_id: str) -> str | None:
        """
        Recebe qualquer número digitado (order ou pack) e retorna ORDER id.
        Se o request falhar (401/429/5xx), não conclui "não é order nem pack" sem log.
        """
        any_id = str(any_id or "").strip()
        if not any_id:
            return None

        # 1) tenta como ORDER direto (com auto-refresh via _request)
        r = self._request("GET", f"/orders/{any_id}")
        if r is None:
            print("❌ ML: sem resposta no /orders (timeout/conn)", flush=True)
            return None

        print("🌐 ML GET /orders status =", r.status_code, flush=True)

        if r.status_code == 200:
            print("🔎 ID já era ORDER", flush=True)
            return any_id

        # Se deu erro de auth/rate-limit, não dá pra afirmar que não é order.
        if r.status_code in (401, 403, 429) or (500 <= r.status_code <= 599):
            print("⚠️ ML /orders falhou (não vou assumir que não é ORDER):", r.status_code, (r.text or "")[:200], flush=True)
            return None

        # 2) tenta como PACK
        r2 = self._request("GET", f"/packs/{any_id}")
        if r2 is None:
            print("❌ ML: sem resposta no /packs (timeout/conn)", flush=True)
            return None

        print("🌐 ML GET /packs status =", r2.status_code, flush=True)

        if r2.status_code == 200:
            pack = r2.json() or {}
            orders = pack.get("orders") or []
            if not orders:
                print("❌ PACK sem orders", flush=True)
                return None
            real_order = str(orders[0]["id"])
            print("🔁 convertido PACK → ORDER:", real_order, flush=True)
            return real_order

        if r2.status_code in (401, 403, 429) or (500 <= r2.status_code <= 599):
            print("⚠️ ML /packs falhou (não vou assumir que não é PACK):", r2.status_code, (r2.text or "")[:200], flush=True)
            return None

        print("❌ não é order nem pack (status orders=", r.status_code, "packs=", r2.status_code, ")", flush=True)
        return None

    def obter_shipping_id(self, pack_id: str):
        """
        Recebe o número que aparece na tela do ML
        e resolve até o shipping_id real
        """

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        # 1) PACK → ORDER REAL
        url_pack = f"https://api.mercadolibre.com/packs/{pack_id}"
        r = requests.get(url_pack, headers=headers, timeout=30)

        if r.status_code != 200:
            raise Exception(f"Erro ML pack: {r.status_code} {r.text}")

        pack = r.json()
        orders = pack.get("orders") or []

        if not orders:
            raise Exception("Pack sem orders")

        real_order_id = orders[0]["id"]
        print("🧾 real_order_id:", real_order_id)

        # 2) ORDER → SHIPPING
        url_order = f"https://api.mercadolibre.com/orders/{real_order_id}"
        r = requests.get(url_order, headers=headers, timeout=30)

        if r.status_code != 200:
            raise Exception(f"Erro ML order: {r.status_code} {r.text}")

        order = r.json()
        shipping_id = (order.get("shipping") or {}).get("id")

        print("📦 shipping_id:", shipping_id)

        return shipping_id
