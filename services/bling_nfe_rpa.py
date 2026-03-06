# services/bling_nfe_rpa.py
from __future__ import annotations

import os
import re
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from services.zpl_converter import ZPLConverterService
from services.windows_print_service import WindowsPrintService
from services.bling_service import BlingService  # ✅ API

BLING_VENDAS_URL = "https://www.bling.com.br/vendas.php#list"
BLING_NOTAS_URL = "https://www.bling.com.br/notas.fiscais.php#list"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


class BlingRPA:
    """
    Fluxo:
    VENDAS -> seleciona pedido -> Gerar NF-e -> (se necessário) Emitir
    -> NOTAS -> seleciona NF -> Enviar NF-es selecionadas -> (confirmar Sim) -> fechar modal (X)
    -> fechar popup processamento -> selecionar NF de novo -> Mais ações
    -> DANFE Simplificado + Etiqueta -> baixar ZPL -> converter -> imprimir
    """

    def __init__(self, headless: bool = True):
        self.headless = headless

    # ============================================================
    # LOGIN (RESILIENTE)
    # ============================================================
    def garantir_logado(self, page):
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(400)

        if "login" not in (page.url or ""):
            return

        print("🔐 Sessão expirada — realizando login automático...")

        user = os.getenv("BLING_USER")
        pwd = os.getenv("BLING_PASS")
        if not user or not pwd:
            raise Exception("BLING_USER / BLING_PASS não definidos no .env")

        page.wait_for_selector("#username", state="visible", timeout=60000)
        page.wait_for_timeout(800)  # importante pro React do Bling

        page.fill("#username", user)
        page.fill("input[type='password']", pwd)

        page.wait_for_function(
            """
            () => {
                const btn = [...document.querySelectorAll('button,div')]
                    .find(e => (e.innerText||'').trim() === 'Entrar');
                return btn && !btn.disabled;
            }
            """,
            timeout=60000,
        )

        page.evaluate(
            """
            () => {
                const btn = [...document.querySelectorAll('button,div')]
                    .find(e => (e.innerText||'').trim() === 'Entrar');
                if (btn) btn.click();
            }
            """
        )

        page.wait_for_function("() => !location.href.includes('login')", timeout=60000)
        page.wait_for_load_state("networkidle")

        print("✅ Login realizado com sucesso")
        page.context.storage_state(path="bling_state.json")

    # ============================================================
    # HELPERS (UI)
    # ============================================================
    def wait_no_overlay(self, page, timeout_ms: int = 180000):
        """Espera o overlay REAL sumir (não só o elemento desaparecer)."""
        try:
            page.wait_for_function(
                """
                () => {
                    const o = document.querySelector('.blockUI.blockOverlay');
                    if (!o) return true;
                    const style = window.getComputedStyle(o);
                    return style.display === 'none' || style.visibility === 'hidden' || o.offsetHeight === 0;
                }
                """,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass

    def _press_escape(self, page):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(150)

    # --------------------------------------------------
    # Seleciona checkbox de uma linha contendo "numero"
    # --------------------------------------------------

    def nf_na_tela_corresponde_ao_pedido(self, page, nf_num: str, numero_pedido: str) -> bool:
        """
        Valida se a NF na tela (NOTAS) pertence ao pedido.
        Faz comparação numérica (40248 == 040248) e aceita variações de tooltip.
        """
        nf_num = re.sub(r"\D+", "", str(nf_num or ""))
        pedido_raw = re.sub(r"\D+", "", str(numero_pedido or ""))

        if not nf_num or not pedido_raw:
            return False

        pedido_int = int(pedido_raw)  # ✅ mata problema de zeros à esquerda

        self.ir_para_lista_notas(page)
        self.wait_no_overlay(page, timeout_ms=60000)

        busca = page.locator("#pesquisa-mini")
        busca.wait_for(state="visible", timeout=60000)
        busca.fill(nf_num)
        busca.press("Enter")
        page.wait_for_timeout(1200)

        page.wait_for_selector("table.tabela-listagem tbody tr", state="attached", timeout=60000)

        rows = page.locator("table.tabela-listagem tbody tr")
        n = rows.count()

        # procura em até 20 linhas (mais que isso geralmente é filtro ruim)
        for i in range(min(n, 20)):
            row = rows.nth(i)

            # tenta achar o "V" laranja (tooltip pode estar em title ou data-original-title)
            v = row.locator(
                "span.bling-v-orange[title], "
                "span.bling-v-orange[data-original-title], "
                "span.bling-v-orange[data-bs-original-title]"
            ).first

            if v.count() == 0:
                continue

            tip = (
                (v.get_attribute("title") or "")
                or (v.get_attribute("data-original-title") or "")
                or (v.get_attribute("data-bs-original-title") or "")
            ).strip()

            if not tip:
                continue

            # ✅ extrai TODOS os números do tooltip e compara numericamente
            nums = [int(x) for x in re.findall(r"\d+", tip)]
            if any(x == pedido_int for x in nums):
                return True

        return False


    def resolver_nf_por_api_com_validacao(
        self,
        page,
        *,
        pv_id: int | None,
        numero_loja: str | None,
        numero_pedido: str,
        timeout_s: int = 180,
        poll_s: float = 2.0,
    ) -> str:
        """
        Resolve NF por API, mas só aceita se a NF na tela pertencer ao pedido.
        Se API vier errada, tenta achar pela TELA (tooltip V).
        """
        t0 = time.time()
        last_nf = None
        last_err = None

        while (time.time() - t0) < float(timeout_s):
            try:
                # 1) tenta resolver por API (curto por tentativa)
                nf_num = self.resolver_numero_nf_por_api(
                    pv_id=pv_id,
                    numero_loja=numero_loja,
                    timeout_s=20,
                    poll_s=poll_s,
                )
                last_nf = nf_num

                # 2) valida na tela se essa NF pertence ao pedido
                if self.nf_na_tela_corresponde_ao_pedido(page, nf_num, numero_pedido):
                    print(f"✅ NF validada na tela: {nf_num} pertence ao pedido {numero_pedido}")
                    return nf_num

                # 3) se não pertence, tenta achar a NF certa pela tela (tooltip V)
                print(f"⚠️ API devolveu NF {nf_num}, mas NÃO pertence ao pedido {numero_pedido}. Tentando achar pela TELA...")
                try:
                    nf_tela = self.achar_nf_do_pedido_na_tela(page, numero_pedido)
                    print(f"✅ NF encontrada pela tela (tooltip V): {nf_tela}")
                    return nf_tela
                except Exception as e:
                    last_err = e
                    print("⚠️ Ainda não achei pela tela, re-tentando...")

            except Exception as e:
                last_err = e

            time.sleep(float(poll_s))

        raise Exception(f"❌ Não consegui achar NF correta. last_nf={last_nf} last_err={last_err}")

    def selecionar_pedido_na_lista(self, page, numero: str):
        """
        Seleciona checkbox da linha cujo texto contém o NUMERO como token EXATO.
        Ex: 075638 NÃO bate em 1075638.
        """
        numero = re.sub(r"\D+", "", str(numero or "").strip())
        if not numero:
            raise Exception("Número inválido para seleção.")

        self._press_escape(page)
        self.wait_no_overlay(page, timeout_ms=60000)

        # garante tabela na tela (vendas/notas usam a mesma estrutura)
        page.wait_for_selector("table.tabela-listagem tbody tr", state="attached", timeout=60000)

        rows = page.locator("table.tabela-listagem tbody tr")
        n = rows.count()
        if n == 0:
            raise Exception("Tabela sem linhas para selecionar.")

        pat = re.compile(rf"(?<!\d){re.escape(numero)}(?!\d)")

        chosen_row = None
        for i in range(min(n, 80)):  # varre até 80 linhas
            r = rows.nth(i)
            try:
                txt = r.inner_text(timeout=1200) or ""
            except Exception:
                continue
            if pat.search(txt):
                chosen_row = r
                break

        if chosen_row is None:
            # debug útil: salva 1ª linha pra entender o que veio
            try:
                first_txt = rows.first.inner_text(timeout=1500)
            except Exception:
                first_txt = "<não consegui ler>"
            raise Exception(f"❌ Não achei linha com token exato {numero}. Primeira linha: {first_txt[:120]}")

        chosen_row.scroll_into_view_if_needed()
        self.wait_no_overlay(page, timeout_ms=30000)

        # ✅ tenta achar o checkbox do Bling (label for="marcado...") dentro da própria linha
        cb_label = chosen_row.locator("label[for^='marcado']").first
        if cb_label.count() > 0:
            cb_label.wait_for(state="visible", timeout=30000)
            for_id = cb_label.get_attribute("for")
            if not for_id:
                raise Exception("Achei label do checkbox, mas sem atributo 'for'.")

            cb_input = page.locator(f"input#{for_id}").first
            cb_input.wait_for(state="attached", timeout=30000)

            try:
                cb_input.check(force=True)
            except Exception:
                page.evaluate(
                    """(el) => {
                        el.checked = true;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    }""",
                    cb_input.element_handle(),
                )

            if not cb_input.is_checked():
                raise Exception(f"❌ Checkbox não ficou marcado ({numero}).")

            print(f"✅ Selecionado na lista (token exato): {numero}")
            return

        # ✅ fallback: algumas telas usam input.tcheck dentro da linha
        cb2 = chosen_row.locator("input.tcheck").first
        if cb2.count() > 0:
            cb2.wait_for(state="attached", timeout=30000)
            try:
                cb2.check(force=True)
            except Exception:
                try:
                    cb2.click(force=True, timeout=3000)
                except Exception:
                    page.evaluate("(el)=>el.click()", cb2.element_handle())

            # tenta validar checked (nem sempre é checkbox real)
            try:
                if hasattr(cb2, "is_checked") and not cb2.is_checked():
                    pass
            except Exception:
                pass

            print(f"✅ Selecionado na lista (fallback tcheck): {numero}")
            return

        raise Exception("❌ Não achei checkbox dentro da linha escolhida (nem label marcado nem input.tcheck).")

    # --------------------------------------------------
    # VENDAS: gerar NF
    # --------------------------------------------------
    def clicar_gerar_nfe_selecionados(self, page):
        btn = page.locator("button.act-gerar-notas").first
        btn.wait_for(state="visible", timeout=30000)
        btn.scroll_into_view_if_needed()
        page.evaluate("(el) => el.click()", btn.element_handle())
        print("✅ Cliquei em gerar NF-e")

    def confirmar_geracao_nfe(self, page):
        print("🟡 Confirmando geração da NF-e...")
        page.wait_for_selector("div.ui-dialog", timeout=30000)

        btn_sim = page.locator("div.ui-dialog button.Button--primary").first
        btn_sim.wait_for(state="visible", timeout=30000)
        page.evaluate("(el) => el.click()", btn_sim.element_handle())
        print("✅ Confirmação enviada!")

    # --------------------------------------------------
    # Modais: confirmar/fechar
    # --------------------------------------------------
    def confirmar_modal_sim(self, page):
        page.wait_for_selector("div.ui-dialog:visible", timeout=30000)
        dialog = page.locator("div.ui-dialog:visible").last
        btn_sim = dialog.locator("button.Button--primary, button:has-text('Sim')").first
        btn_sim.wait_for(state="visible", timeout=30000)
        try:
            btn_sim.click(timeout=3000)
        except Exception:
            page.evaluate("(el) => el.click()", btn_sim.element_handle())
        print("✅ Confirmei (Sim)")

    def fechar_modal_no_x(self, page):
        print("❎ Fechando modal no X...")
        dialog = page.locator("div.ui-dialog:visible").last
        dialog.wait_for(state="visible", timeout=60000)

        x = dialog.locator("button.ui-dialog-titlebar-close").first
        x.wait_for(state="visible", timeout=60000)

        try:
            x.click(timeout=3000)
        except Exception:
            page.evaluate("(el) => el.click()", x.element_handle())

        try:
            dialog.wait_for(state="hidden", timeout=30000)
        except Exception:
            pass

        self.wait_no_overlay(page, timeout_ms=60000)
        print("✅ Modal fechado")

    def fechar_popup_processamento(self, page):
        """
        O Bling às vezes mostra popup de processamento, às vezes não.
        Essa função NÃO pode travar o fluxo.
        """
        print("🔎 Verificando popup de processamento...")

        try:
            page.wait_for_timeout(1500)

            close_btn = page.locator("button.ui-dialog-titlebar-close:visible")
            if close_btn.count() == 0:
                print("ℹ️ Nenhum popup apareceu — seguindo fluxo")
                return

            print("❎ Popup detectado — fechando...")
            close_btn.last.click(timeout=3000)

            self.wait_no_overlay(page, timeout_ms=30000)
            print("✅ Popup fechado")

        except Exception:
            print("ℹ️ Popup não interferiu no fluxo — continuando")

    # ============================================================
    # NOTAS: garantir que está na lista CERTA
    # ============================================================

    def achar_nf_do_pedido_na_tela(self, page, numero_pedido: str) -> str:
        pedido = re.sub(r"\D+", "", str(numero_pedido or ""))
        if not pedido:
            raise Exception("Pedido inválido para achar NF na tela.")

        self.ir_para_lista_notas(page)
        self.wait_no_overlay(page, timeout_ms=60000)

        busca = page.locator("#pesquisa-mini")
        busca.wait_for(state="visible", timeout=60000)
        busca.fill(pedido)
        busca.press("Enter")
        page.wait_for_timeout(1200)

        page.wait_for_selector("table.tabela-listagem tbody tr", state="attached", timeout=60000)

        rows = page.locator("table.tabela-listagem tbody tr")
        n = rows.count()

        alvo = f"Número pedido: {pedido}"

        for i in range(min(n, 30)):
            row = rows.nth(i)

            v = row.locator(
                "span.bling-v-orange[title], "
                "span.bling-v-orange[data-original-title], "
                "span.bling-v-orange[data-bs-original-title]"
            ).first

            if v.count() == 0:
                continue

            tip = (
                (v.get_attribute("title") or "")
                or (v.get_attribute("data-original-title") or "")
                or (v.get_attribute("data-bs-original-title") or "")
            ).strip()

            if not tip or (alvo not in tip):
                continue

            txt = row.inner_text(timeout=1500) or ""
            m = re.search(r"(?<!\d)(0\d{5,})(?!\d)", txt)
            if m:
                return m.group(1)

        raise Exception(f"❌ Não achei NF na tela para o pedido {pedido} (tooltip V).")

    def ir_para_lista_notas(self, page, timeout_ms: int = 120000):
        if "notas.fiscais.php" not in (page.url or ""):
            page.goto(BLING_NOTAS_URL, wait_until="domcontentloaded", timeout=timeout_ms)

        self.garantir_logado(page)
        self.wait_no_overlay(page, timeout_ms=timeout_ms)

        page.wait_for_selector("#pesquisa-mini", state="visible", timeout=timeout_ms)
        page.wait_for_selector("table.tabela-listagem", state="attached", timeout=timeout_ms)
        page.wait_for_selector("table.tabela-listagem tbody", state="attached", timeout=timeout_ms)

        print("📄 Tela de notas fiscais pronta (URL + tabela OK)")

    def selecionar_para_imprimir(self, page, numero_nf: str):
        self.wait_no_overlay(page, timeout_ms=60000)

        busca = page.locator("#pesquisa-mini")
        busca.wait_for(state="visible", timeout=60000)
        busca.fill(str(numero_nf))
        busca.press("Enter")
        page.wait_for_timeout(1200)

        self.selecionar_pedido_na_lista(page, numero_nf)
        self.garantir_selecao_reconhecida(page)

    # ============================================================
    # ✅ RESOLVE NF POR API (PV -> NFE -> NUMERO)
    # ============================================================
    def resolver_numero_nf_por_api(
        self,
        *,
        pv_id: int | None,
        numero_loja: str | None,
        timeout_s: int = 180,
        poll_s: float = 2.0,
    ) -> str:
        """
        Resolve o número da NF (010xxx) usando vínculo forte:
          PV_ID -> (acha NFE_ID vinculada) -> (pega numero da NF)

        ✅ Não depende do DOM/tabela do Bling (que em headless tá falhando).
        """
        if not pv_id:
            raise Exception("PV_ID não veio — sem PV não dá pra resolver NF por API.")

        bling = BlingService()

        t0 = time.time()
        last_err = None

        while (time.time() - t0) < float(timeout_s):
            try:
                nfe_id = bling.resolver_nfe_por_pv_id(int(pv_id), numero_loja=str(numero_loja or "").strip() or None)
                if nfe_id:
                    numero = bling.obter_numero_nota(int(nfe_id))
                    if numero:
                        nf_num = re.sub(r"\D+", "", str(numero))
                        if nf_num:
                            return nf_num
            except Exception as e:
                last_err = e

            time.sleep(float(poll_s))

        raise Exception(
            f"❌ API: não consegui resolver número da NF para PV={pv_id} em {timeout_s}s. last_err={last_err}"
        )

    # ============================================================
    # DOM fallback (se você não passar PV)
    # ============================================================
    def aguardar_notas_e_pegar_numero_nf(self, page, numero_pedido: str) -> str:
        """
        Fallback antigo (DOM/tabela).
        Mantido pra você não travar caso chame emitir() sem pv_id.
        """
        pedido_str = str(numero_pedido).strip()
        print("📄 Tentando pegar número da NF na lista (fallback DOM)...")

        self.ir_para_lista_notas(page)

        deadline = time.time() + 180  # 3 min total
        tentativa = 0

        while time.time() < deadline:
            tentativa += 1
            print(f"🧾 NOTAS: tentativa {tentativa} de localizar NF do pedido {pedido_str}...")

            try:
                self.wait_no_overlay(page, timeout_ms=30000)

                busca = page.locator("#pesquisa-mini")
                busca.wait_for(state="visible", timeout=60000)
                busca.fill(pedido_str)
                busca.press("Enter")
                page.wait_for_timeout(1200)

                page.wait_for_selector("table.tabela-listagem", state="attached", timeout=60000)

                rows = page.locator("table.tabela-listagem tbody tr")
                ok_rows = False
                for _ in range(12):  # ~12s
                    self.wait_no_overlay(page, timeout_ms=15000)
                    if rows.count() > 0:
                        ok_rows = True
                        break
                    page.wait_for_timeout(1000)

                if not ok_rows:
                    raise Exception("Tabela sem linhas (ainda).")

                idx_num = page.evaluate(
                    """
                    () => {
                        const ths = Array.from(document.querySelectorAll("table.tabela-listagem thead tr th"));
                        const norm = (s) => (s||"").replace(/\\s+/g," ").trim().toLowerCase();
                        const targets = ["número","numero","nº","n°","no."];
                        for (let i = 0; i < ths.length; i++) {
                            const t = norm(ths[i].innerText);
                            if (targets.some(x => t.includes(x))) return i;
                        }
                        return -1;
                    }
                    """
                )
                if idx_num is None or int(idx_num) < 0:
                    raise Exception("Não achei a coluna 'Número' no cabeçalho.")
                idx_num = int(idx_num)

                n = rows.count()
                pat = re.compile(rf"(?<!\\d){re.escape(pedido_str)}(?!\\d)")
                chosen = None

                for i in range(min(n, 30)):
                    try:
                        txt = rows.nth(i).inner_text(timeout=1500)
                    except Exception:
                        continue
                    if pat.search(txt or ""):
                        chosen = rows.nth(i)
                        break

                if chosen is None:
                    chosen = rows.first

                txt_num = chosen.locator("td").nth(idx_num).inner_text(timeout=3000).strip()
                nf_num = re.sub(r"\\D+", "", txt_num)

                if not nf_num:
                    raise Exception(f"Não extraí NF pela coluna Número. (texto='{txt_num}')")

                print(f"✅ Número da NF capturado (pedido {pedido_str}): {nf_num}")
                return nf_num

            except Exception as e:
                print(f"⚠️ NOTAS: falhou tentativa {tentativa}: {e}")
                page.wait_for_timeout(2500)

        # debug
        try:
            DOWNLOADS_DIR
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            snap = os.path.join(DOWNLOADS_DIR, f"debug_nf_nao_encontrada_{int(time.time())}.png")
            page.screenshot(path=snap, full_page=True)
            print("📸 Screenshot salvo:", snap)
        except Exception:
            pass

        raise Exception(f"❌ Não consegui localizar a NF do pedido {pedido_str} dentro do tempo limite.")

    # ============================================================
    # ENVIAR NF-e SELECIONADA
    # ============================================================
    def clicar_enviar_nfes_menu(self, page):
        print("📤 Abrindo ação 'Enviar NF-es selecionadas'...")

        candidates = [
            "span.action-text:has-text('Enviar NF-es selecionadas')",
            "text=Enviar NF-es selecionadas",
            "[title='Enviar notas selecionadas']",
            "[data-original-title='Enviar notas selecionadas']",
            "[title='Enviar NF-es selecionadas']",
            "button[title='Enviar NF-es selecionadas']",
        ]

        for sel in candidates:
            loc = page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=6000)
                loc.scroll_into_view_if_needed()
                page.evaluate("(el) => el.click()", loc.element_handle())
                print("✅ Cliquei em 'Enviar NF-es selecionadas'")
                return True
            except Exception:
                continue

        ok = page.evaluate(
            """
            () => {
                const norm = (s) => (s||"").replace(/\\s+/g," ").trim().toLowerCase();
                const targets = ["enviar nf-es selecionadas","enviar notas selecionadas","enviar selecionadas","enviar"];
                const els = Array.from(document.querySelectorAll("span,button,a,div"));
                const hit = els.find(el => el.offsetParent !== null && targets.some(t => norm(el.innerText).includes(t)));
                if (hit) { hit.click(); return true; }
                return false;
            }
            """
        )
        if ok:
            print("✅ Cliquei em 'Enviar NF-es selecionadas' (fallback JS)")
            return True

        return False

    def abrir_acao_enviar(self, page) -> bool:
        try:
            if page.locator("#notaAcao:visible").count() > 0:
                print("✅ #notaAcao já está visível (não precisei abrir menu)")
                return True
        except Exception:
            pass

        if self.clicar_enviar_nfes_menu(page):
            return True

        ok = page.evaluate(
            """
            () => {
                const norm = (s) => (s||"").replace(/\\s+/g," ").trim().toLowerCase();
                const vis = (el) => el && el.offsetParent !== null;

                const openSelectors = [
                    ".more-actions .open-more-actions",
                    ".open-more-actions",
                    "button[title*='Ações']",
                    ".actions button"
                ];
                for (const sel of openSelectors) {
                    const el = document.querySelector(sel);
                    if (vis(el)) { el.click(); break; }
                }

                const targets = ["enviar nf", "enviar notas", "enviar selecionadas", "enviar"];
                const els = Array.from(document.querySelectorAll("span,button,a,div"));
                const hit = els.find(el => vis(el) && targets.some(t => norm(el.innerText).includes(t)));
                if (hit) { hit.click(); return true; }
                return false;
            }
            """
        )
        if ok:
            print("✅ Ação de enviar aberta via fallback JS")
            return True

        return False

    def clicar_enviar_selecionadas(self, page):
        print("📨 Clicando em 'Enviar selecionadas' (#notaAcao)...")

        self.wait_no_overlay(page, timeout_ms=60000)

        btn = page.locator("#notaAcao:visible, button:has-text('Enviar selecionadas'):visible").first
        try:
            btn.wait_for(state="visible", timeout=8000)
            btn.scroll_into_view_if_needed()
            try:
                btn.click(timeout=3000)
            except Exception:
                page.evaluate("(el) => el.click()", btn.element_handle())
            print("✅ Cliquei em 'Enviar selecionadas' (global)")
            return
        except Exception:
            pass

        dialog = page.locator("div.ui-dialog:visible").last
        dialog.wait_for(state="visible", timeout=60000)

        btn2 = dialog.locator("#notaAcao, button:has-text('Enviar selecionadas')").first
        btn2.wait_for(state="visible", timeout=60000)
        btn2.scroll_into_view_if_needed()
        try:
            btn2.click(timeout=3000)
        except Exception:
            page.evaluate("(el) => el.click()", btn2.element_handle())

        print("✅ Cliquei em 'Enviar selecionadas' (modal)")

    def enviar_nf_selecionada_fluxo_completo(self, page):
        print("📤 Abrindo fluxo de envio...")

        if not self.abrir_acao_enviar(page):
            raise Exception("❌ Não consegui abrir ação de envio (menu/botão).")

        try:
            page.wait_for_selector("div.ui-dialog:visible", timeout=15000)
        except Exception:
            pass

        self.clicar_enviar_selecionadas(page)

        try:
            self.confirmar_modal_sim(page)
        except Exception:
            pass

        print("⏳ Aguardando Bling processar envio...")
        page.wait_for_timeout(2200)

        try:
            self.fechar_modal_no_x(page)
        except Exception:
            pass

    # ============================================================
    # MAIS AÇÕES -> IMPRIMIR COMBO
    # ============================================================
    def abrir_mais_acoes(self, page):
        print("➕ Abrindo 'Mais ações' (forçado + interativo)...")

        self.wait_no_overlay(page, timeout_ms=60000)
        self._press_escape(page)

        ok = page.evaluate(
            """
            () => {
                const root = document.querySelector('.more-actions');
                if (!root) return false;

                const hidden = root.querySelector('.hidden-actions');
                if (!hidden) return false;

                hidden.hidden = false;
                hidden.style.display = 'block';
                hidden.style.visibility = 'visible';
                hidden.style.opacity = '1';
                hidden.style.pointerEvents = 'auto';
                hidden.style.zIndex = '99999';
                hidden.classList.add('force-open');

                const ul = hidden.querySelector('ul');
                if (ul) {
                    ul.style.pointerEvents = 'auto';
                    ul.style.zIndex = '99999';
                }

                return true;
            }
            """
        )
        if not ok:
            raise Exception("❌ Não consegui forçar abrir o menu '.more-actions'.")

        page.wait_for_selector(".more-actions .hidden-actions.force-open", timeout=10000)

        # garante que o item existe antes de tentar clicar depois
        page.locator("li[data-function='imprimirDANFESimplificadoComEtiqueta']").first.wait_for(
            state="attached",
            timeout=10000
        )

        print("✅ Menu 'Mais ações' aberto e item do combo detectado")

    def clicar_danfe_simplificado_com_etiqueta(self, page):
        print("🖨️ Tentando clicar: DANFE Simplificado + Etiqueta...")

        self.wait_no_overlay(page, timeout_ms=60000)

        # garante menu aberto e item existe no DOM
        item = page.locator("li[data-function='imprimirDANFESimplificadoComEtiqueta']").first
        item.wait_for(state="attached", timeout=20000)

        def zpl_apareceu() -> bool:
            try:
                if page.locator("text=Impressões geradas em zpl").first.count() > 0:
                    return True
                if page.locator("a:has-text('Clique para baixar os arquivos')").first.count() > 0:
                    return True
            except Exception:
                pass
            return False

        for tentativa in range(1, 4):
            print(f"🧪 Tentativa {tentativa}/3 de acionar impressão ZPL...")

            self.wait_no_overlay(page, timeout_ms=60000)

            # 1) click normal/force no elemento
            try:
                item.scroll_into_view_if_needed()
                item.click(timeout=4000, force=True)
            except Exception:
                pass

            page.wait_for_timeout(500)
            self.wait_no_overlay(page, timeout_ms=30000)

            # 2) click via JS (muito melhor no headless)
            if not zpl_apareceu():
                try:
                    page.evaluate("""
                        () => {
                            const el = document.querySelector("li[data-function='imprimirDANFESimplificadoComEtiqueta']");
                            if (!el) return false;
                            el.scrollIntoView({block:'center'});
                            el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true}));
                            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true}));
                            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                            return true;
                        }
                    """)
                except Exception:
                    pass

            # 3) clique por coordenada (último recurso)
            if not zpl_apareceu():
                try:
                    box = item.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                except Exception:
                    pass

            # espera o card/link aparecer (às vezes demora mais em headless)
            t0 = time.time()
            while time.time() - t0 < 12:
                if zpl_apareceu():
                    print("✅ Clique funcionou: card de ZPL apareceu.")
                    return
                page.wait_for_timeout(300)

            # se não apareceu, fecha e reabre menu pra evitar estado bugado
            self._press_escape(page)
            page.wait_for_timeout(200)

        raise Exception("❌ Não consegui acionar o DANFE Simplificado + Etiqueta (o card/link de ZPL não apareceu).")

    def garantir_selecao_reconhecida(self, page, timeout_ms: int = 20000):
        """
        O Bling só habilita ações quando ele reconhece a seleção.
        Esse helper não pode travar se o texto estiver quebrado em spans.

        Critérios de OK:
        1) achar "Selecionados" + um número no innerText da página
        2) OU detectar algum checkbox marcado no DOM
        """
        t0 = time.time()
        last = ""

        def _extract_selected_count(txt: str) -> int | None:
            if not txt:
                return None

            # pega caso "Selecionados: 1 cadastro"
            m = re.search(r"Selecionados\s*:?\s*(\d+)", txt, flags=re.IGNORECASE)
            if m:
                return int(m.group(1))

            # pega caso quebrado tipo:
            # "Selecionados" \n "1"
            m2 = re.search(r"Selecionados\s+(\d+)", txt, flags=re.IGNORECASE)
            if m2:
                return int(m2.group(1))

            return None

        while (time.time() - t0) * 1000 < timeout_ms:
            # 1) tenta pelo texto do body inteiro (mais confiável que locator)
            try:
                body_text = page.evaluate("() => document.body ? (document.body.innerText || '') : ''") or ""
                if body_text:
                    last = body_text
                    n = _extract_selected_count(body_text)
                    if n is not None and n > 0:
                        print(f"✅ Bling reconheceu seleção (texto): Selecionados={n}")
                        return True
            except Exception:
                pass

            # 2) tenta pela contagem de checkbox marcado (Bling usa label/for -> input#marcado...)
            try:
                checked = page.evaluate(
                    """
                    () => {
                        const a = document.querySelectorAll("input[id^='marcado']:checked").length;
                        const b = document.querySelectorAll("input.tcheck:checked").length;
                        return a + b;
                    }
                    """
                )
                if isinstance(checked, int) and checked > 0:
                    print(f"✅ Bling reconheceu seleção (checkbox marcado): {checked}")
                    return True
            except Exception:
                pass

            page.wait_for_timeout(250)

        # Se chegou aqui, não achou pelo texto.
        # Antes de travar, tenta um último sanity-check: se tiver checkbox marcado, segue.
        try:
            checked = page.evaluate(
                "() => document.querySelectorAll(\"input[id^='marcado']:checked, input.tcheck:checked\").length"
            )
            if isinstance(checked, int) and checked > 0:
                print("⚠️ Não achei 'Selecionados' no texto, mas há checkbox marcado — seguindo fluxo.")
                return True
        except Exception:
            pass

        raise Exception(f"❌ Bling NÃO reconheceu a seleção (não consegui detectar selecionados>0). last='{(last or '')[:500]}'")

    # ============================================================
    # DOWNLOAD ZPL -> PDF -> IMPRIMIR
    # ============================================================

    def _baixar_combo_zpl(self, page, nome_base: str) -> str:
        DOWNLOADS_DIR
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        saida = os.path.join(DOWNLOADS_DIR, f"{nome_base}_{int(time.time())}.zpl")

        def salvar_bytes(b: bytes) -> str:
            if not b or len(b) < 20:
                raise Exception("❌ Arquivo veio vazio/curto demais")
            with open(saida, "wb") as f:
                f.write(b)
            print("✅ ZPL salvo:", saida)
            return saida

        print("⬇️ Aguardando ZPL real aparecer no DOM...")

        # 👇 ESPERA QUALQUER LINK DE DOWNLOAD EXISTIR
        page.wait_for_function("""
            () => {
                const links = document.querySelectorAll('a[href*="security.file.php"]');
                return links.length > 0;
            }
        """, timeout=180000)

        href = page.eval_on_selector(
            'a[href*="security.file.php"]',
            'a => a.href'
        )

        if not href:
            raise Exception("❌ Não consegui capturar o link do ZPL")

        print("🔗 HREF capturado:", href[:120], "...")

        # baixa usando sessão autenticada
        resp = page.context.request.get(href, timeout=180000)
        if not resp.ok:
            raise Exception(f"❌ Download falhou: {resp.status} - {resp.status_text()}")

        body = resp.body()

        # Bling às vezes devolve HTML primeiro
        if body[:20].lstrip().startswith(b"<"):
            html = body.decode("utf-8", errors="ignore")
            m = re.search(r"https://www\.bling\.com\.br/security\.file\.php\?token=[^\"']+", html)
            if m:
                print("↪️ Redirecionamento detectado — tentando link final...")
                resp2 = page.context.request.get(m.group(0), timeout=180000)
                if resp2.ok:
                    body = resp2.body()

        return salvar_bytes(body)

    def _converter_zpl_para_pdf(self, zpl_path: str, nome_base: str) -> str:
        DOWNLOADS_DIR
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        with open(zpl_path, "rb") as f:
            zpl_string = f.read().decode("utf-8", errors="ignore")

        print("🧠 Convertendo ZPL em PDF (multi-page)...")
        pdf_bytes = ZPLConverterService.zpl_to_pdf(zpl_string)

        saida_pdf = os.path.join(DOWNLOADS_DIR, f"{nome_base}_{int(time.time())}.pdf")
        with open(saida_pdf, "wb") as f:
            f.write(pdf_bytes)

        print(f"✅ PDF convertido salvo em: {saida_pdf}")
        return saida_pdf

    def _imprimir_pdf(self, pdf_path: str):
        print("PDF pronto — navegador vai imprimir:", pdf_path)

    # ============================================================
    # FLUXO DIRETO POR NF
    # ============================================================
    def imprimir_combo_por_numero_nf(self, numero_nf: str):
        print("🧾 Fluxo direto por número da NF:", numero_nf)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless, args=["--start-maximized"])
            context = browser.new_context(
                storage_state="bling_state.json",
                viewport={"width": 1366, "height": 900},
                accept_downloads=True,
            )
            page = context.new_page()

            page.goto(BLING_NOTAS_URL, wait_until="domcontentloaded", timeout=120000)
            self.garantir_logado(page)
            self.ir_para_lista_notas(page)

            busca = page.locator("#pesquisa-mini")
            busca.wait_for(state="visible", timeout=60000)
            busca.fill(str(numero_nf))
            busca.press("Enter")
            page.wait_for_timeout(1200)

            self.selecionar_pedido_na_lista(page, numero_nf)
            self.garantir_selecao_reconhecida(page)  # ✅ faltava aqui
            page.wait_for_timeout(300)

            self.enviar_nf_selecionada_fluxo_completo(page)

            try:
                self.fechar_popup_processamento(page)
            except Exception:
                pass

            self.selecionar_para_imprimir(page, numero_nf)

            self.abrir_mais_acoes(page)
            self.wait_no_overlay(page, timeout_ms=30000)  # ✅ ajuda no headless
            self.clicar_danfe_simplificado_com_etiqueta(page)

            page.wait_for_timeout(800)
            page.locator("text=Impressões geradas em zpl").first.wait_for(timeout=60000)
            page.locator("a:has-text('Clique para baixar os arquivos')").first.wait_for(timeout=60000)

            zpl_path = self._baixar_combo_zpl(page, f"combo_{numero_nf}")
            pdf_path = self._converter_zpl_para_pdf(zpl_path, f"combo_{numero_nf}")
            return {
                "pdf": pdf_path
            }

            print("✅ Fluxo direto finalizado — combo impresso")

    # ============================================================
    # ✅ EMITIR COMPLETO (VENDAS -> NOTAS) COM NF POR API
    # ============================================================
    def emitir(self, numero_pedido: str, pv_id: int | None = None, numero_loja: str | None = None):
        """
        Fluxo completo começando em VENDAS pelo número visível.

        ✅ Diferença chave:
        - o número da NF (010xxx) é resolvido via API pelo vínculo PV->NFE,
          sem depender da tabela/DOM do Bling que tá falhando no headless.

        Se pv_id NÃO vier, cai no fallback DOM (seu método antigo).
        """
        print("🤖 Iniciando navegador...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless, args=["--start-maximized"])
            context = browser.new_context(
                storage_state="bling_state.json",
                viewport={"width": 1366, "height": 900},
                accept_downloads=True,
            )
            page = context.new_page()

            # ---------------------------
            # VENDAS
            # ---------------------------
            print("🌐 Abrindo página de vendas...")
            page.goto(BLING_VENDAS_URL, wait_until="domcontentloaded", timeout=120000)
            self.garantir_logado(page)
            self.wait_no_overlay(page, timeout_ms=60000)

            page.wait_for_selector("#pesquisa-mini", timeout=60000)

            print(f"🔎 Buscando pedido {numero_pedido} ...")
            busca = page.locator("#pesquisa-mini")
            busca.fill(str(numero_pedido))
            busca.press("Enter")
            page.wait_for_timeout(1200)

            self.selecionar_pedido_na_lista(page, numero_pedido)

            self.clicar_gerar_nfe_selecionados(page)
            self.confirmar_geracao_nfe(page)

            print("⏳ Detectando comportamento do Bling...")
            try:
                page.wait_for_selector("text=Emitir nota fiscal", timeout=8000)
                print("🧾 Tela de emissão detectada — emitindo")
                page.get_by_role("button", name="Emitir").click()
                print("⏳ aguardando SEFAZ...")
                page.wait_for_selector("text=Autorizada", timeout=180000)
            except Exception:
                print("⚠️ Bling pulou a tela de emissão (NF já criada ou automática)")

            # ---------------------------
            # NOTAS
            # ---------------------------
            self.ir_para_lista_notas(page)

            # ✅ pega NF via API se tiver PV
            if pv_id:
                print("🔗 Resolvendo número da NF via API + validação na tela (pedido -> NF)...")
                nf_num = self.resolver_nf_por_api_com_validacao(
                    page,
                    pv_id=pv_id,
                    numero_loja=numero_loja,
                    numero_pedido=str(numero_pedido),
                    timeout_s=180,
                    poll_s=2.0,
                )
                print("✅ Número da NF (API VALIDADA) =", nf_num)
            else:
                print("⚠️ pv_id não informado — usando fallback DOM pra achar a NF na lista...")
                nf_num = self.aguardar_notas_e_pegar_numero_nf(page, numero_pedido)
            # filtra e seleciona NF (010xxx)
            busca = page.locator("#pesquisa-mini")
            busca.fill(str(nf_num))
            busca.press("Enter")
            page.wait_for_timeout(1200)

            self.selecionar_pedido_na_lista(page, nf_num)
            self.garantir_selecao_reconhecida(page)
            # enviar
            self.enviar_nf_selecionada_fluxo_completo(page)

            try:
                self.fechar_popup_processamento(page)
            except Exception:
                pass

            # re-seleciona
            self.selecionar_para_imprimir(page, nf_num)

            # Mais ações -> combo
            self.abrir_mais_acoes(page)
            self.clicar_danfe_simplificado_com_etiqueta(page)

            # Download -> PDF -> imprimir
            zpl_path = self._baixar_combo_zpl(page, f"combo_{nf_num}")
            pdf_path = self._converter_zpl_para_pdf(zpl_path, f"combo_{nf_num}")

            filename = os.path.basename(pdf_path)
            return {"pdf": f"/print/{filename}"}

            print("🎯 FINAL PERFEITO — NF enviada + DANFE simplificado + etiqueta impressos")

    # ============================================================
    # DEBUGS (mantidos)
    # ============================================================
    def debug_combo_baixar_sem_imprimir(self, numero_nf: str):
        """
        Abre navegador VISÍVEL
        Faz TODO fluxo até baixar o ZPL
        Depois converte e imprime
        """
        print("🧪 DEBUG COM DOWNLOAD + IMPRESSÃO")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(
                storage_state="bling_state.json",
                viewport={"width": 1366, "height": 900},
                accept_downloads=True,
            )
            page = context.new_page()

            page.goto(BLING_NOTAS_URL, wait_until="domcontentloaded", timeout=120000)
            self.garantir_logado(page)
            self.ir_para_lista_notas(page)

            busca = page.locator("#pesquisa-mini")
            busca.wait_for(state="visible", timeout=60000)
            busca.fill(str(numero_nf))
            busca.press("Enter")
            page.wait_for_timeout(1200)

            self.selecionar_pedido_na_lista(page, numero_nf)

            self.enviar_nf_selecionada_fluxo_completo(page)

            try:
                self.fechar_popup_processamento(page)
            except Exception:
                pass

            self.selecionar_para_imprimir(page, numero_nf)

            self.abrir_mais_acoes(page)
            self.clicar_danfe_simplificado_com_etiqueta(page)

            print("⬇️ Aguardando tela/link de download do Bling...")
            zpl_path = self._baixar_combo_zpl(page, f"combo_{numero_nf}")
            print("📦 BAIXOU! Arquivo salvo em:", zpl_path)

            pdf_path = self._converter_zpl_para_pdf(zpl_path, f"combo_{numero_nf}")
            self._imprimir_pdf(pdf_path)

            print("🖨️ IMPRESSO! PDF:", pdf_path)
            print("👀 Segurando 20s pra você ver…")
            page.wait_for_timeout(20000)

            browser.close()

    def debug_imprimir_combo(self, numero_nf: str):
        """
        Abre o navegador visível e executa:
        selecionar NF -> enviar -> mais ações -> danfe simplificado + etiqueta

        NÃO baixa arquivo
        NÃO imprime
        """
        print("🧪 MODO DEBUG VISUAL — nenhuma impressão será feita")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(
                storage_state="bling_state.json",
                viewport={"width": 1366, "height": 900},
                accept_downloads=True,
            )
            page = context.new_page()

            page.goto(BLING_NOTAS_URL, wait_until="domcontentloaded", timeout=120000)
            self.garantir_logado(page)
            self.ir_para_lista_notas(page)

            busca = page.locator("#pesquisa-mini")
            busca.wait_for(state="visible", timeout=60000)
            busca.fill(str(numero_nf))
            busca.press("Enter")
            page.wait_for_timeout(1200)

            self.selecionar_pedido_na_lista(page, numero_nf)
            self.garantir_selecao_reconhecida(page)
            page.wait_for_timeout(600)

            self.enviar_nf_selecionada_fluxo_completo(page)

            try:
                self.fechar_popup_processamento(page)
            except Exception:
                pass

            self.selecionar_para_imprimir(page, numero_nf)

            self.abrir_mais_acoes(page)
            self.clicar_danfe_simplificado_com_etiqueta(page)

            print("👀 DEBUG concluído — observe a tela por 40s")
            page.wait_for_timeout(40000)
            browser.close()