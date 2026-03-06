# services/bling_estrutura_robo.py
from __future__ import annotations

import os
from typing import List, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BLING_PRODUTOS_URL = "https://www.bling.com.br/produtos.php"


def garantir_logado(page) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    page.wait_for_timeout(400)

    # se não caiu no login → segue normal
    if "login" not in (page.url or ""):
        return

    print("🔐 Sessão do Bling expirada — fazendo login automático...")

    user = os.getenv("BLING_USER")
    pwd = os.getenv("BLING_PASS")

    if not user or not pwd:
        raise Exception("BLING_USER / BLING_PASS não definidos no .env")

    # espera React montar tela
    page.wait_for_selector("#username", state="visible", timeout=60000)
    page.wait_for_timeout(800)

    page.fill("#username", user)
    page.fill("input[type='password']", pwd)

    # botão entrar do bling é esquisito (não é <button> normal)
    page.wait_for_function("""
        () => {
            const btn = [...document.querySelectorAll('button,div')]
                .find(e => (e.innerText||'').trim() === 'Entrar');
            return btn && !btn.disabled;
        }
    """, timeout=60000)

    page.evaluate("""
        () => {
            const btn = [...document.querySelectorAll('button,div')]
                .find(e => (e.innerText||'').trim() === 'Entrar');
            if (btn) btn.click();
        }
    """)

    page.wait_for_function("() => !location.href.includes('login')", timeout=60000)
    page.wait_for_load_state("networkidle")

    print("✅ Login automático realizado")

    # 🔥 MUITO IMPORTANTE
    page.context.storage_state(path="bling_state.json")


def _produto_abriu(page) -> bool:
    # 1) se saiu da URL clássica, ok
    try:
        if page.evaluate("() => !location.href.includes('produtos.php')"):
            return True
    except Exception:
        pass

    # 2) mesmo na mesma URL, se DOM de edição apareceu, ok
    try:
        return bool(
            page.evaluate(
                """
            () => {
                if (document.querySelector("#formProduto")) return true;
                if (document.querySelector("ul.bling-tabs")) return true;
                if (document.querySelector("li[data-tab='div_estrutura']")) return true;

                const anyInput = document.querySelector("input[name='descricao'], input[name='codigo'], input[name='gtin']");
                if (anyInput) return true;

                return false;
            }
        """
            )
        )
    except Exception:
        return False


def _abrir_produto_pela_linha(page, row, sku: str, timeout_ms: int = 20000) -> bool:
    """
    Abre a tela do produto a partir de uma linha da listagem.
    NÃO depende de URL mudar.
    """

    def espera_produto():
        page.wait_for_function(
            "() => ("
            "!!document.querySelector('#formProduto') || "
            "!!document.querySelector('ul.bling-tabs') || "
            "!!document.querySelector(\"li[data-tab='div_estrutura']\")"
            ")",
            timeout=timeout_ms,
        )

    # garante linha visível
    try:
        row.scroll_into_view_if_needed()
    except Exception:
        pass

    # 0) tenta clicar em algum link visível
    try:
        a = row.locator("a:visible").first
        if a.count() > 0:
            a.click(timeout=3000)
            page.wait_for_timeout(300)
            if _produto_abriu(page):
                return True
            try:
                espera_produto()
                return True
            except Exception:
                pass
    except Exception:
        pass

    # 1) dblclick na linha
    try:
        row.dblclick(timeout=3000)
        page.wait_for_timeout(400)
        if _produto_abriu(page):
            return True
        try:
            espera_produto()
            return True
        except Exception:
            pass
    except Exception:
        pass

    # 2) click em célula (geralmente a descrição)
    try:
        cell = row.locator("td").nth(1)
        cell.click(timeout=3000)
        page.wait_for_timeout(250)
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)

        if _produto_abriu(page):
            return True
        try:
            espera_produto()
            return True
        except Exception:
            pass
    except Exception:
        pass

    # 3) coordenada
    try:
        box = row.bounding_box()
        if box:
            page.mouse.click(
                box["x"] + box["width"] * 0.30,
                box["y"] + box["height"] * 0.50,
            )
            page.wait_for_timeout(250)
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)

            if _produto_abriu(page):
                return True
            try:
                espera_produto()
                return True
            except Exception:
                pass
    except Exception:
        pass

    # 4) JS fallback: acha a row pelo texto e clica nela + enter
    try:
        ok = page.evaluate(
            """(sku) => {
                const rows = Array.from(document.querySelectorAll("table.tabela-listagem tbody tr"));
                const target = rows.find(r => (r.innerText || "").includes(String(sku)));
                if (!target) return false;
                target.scrollIntoView({block:'center'});
                target.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                return true;
            }""",
            sku,
        )
        if ok:
            page.wait_for_timeout(200)
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            page.wait_for_timeout(600)

            if _produto_abriu(page):
                return True
            try:
                espera_produto()
                return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def abrir_aba_estrutura(page, timeout_ms: int = 20000) -> bool:
    """
    Clica na aba Estrutura e espera a tabela aparecer.
    Versão simples e estável (funciona no seu layout).
    """

    try:
        page.wait_for_selector("li[data-tab='div_estrutura']", timeout=timeout_ms)

        # clica direto no LI (como antes)
        page.locator("li[data-tab='div_estrutura']").first.click(force=True)

        # espera a tabela realmente aparecer
        page.wait_for_function("""
            () => {
                const host = document.querySelector("#div_estrutura");
                if (!host) return false;
                const rows = host.querySelectorAll("table tbody tr");
                return rows.length > 0;
            }
        """, timeout=timeout_ms)

        return True

    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False

    def conteudo_visivel() -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                () => {
                    const host = document.querySelector("#div_estrutura, div#div_estrutura");
                    if (!host) return false;
                    const s = getComputedStyle(host);
                    if (s.display === "none" || s.visibility === "hidden") return false;
                    return host.offsetHeight > 0;
                }
            """
                )
            )
        except Exception:
            return False

    # se nem existe a aba, sai
    try:
        if li.count() == 0 and a.count() == 0:
            return False
    except Exception:
        return False

    # 1) click no LI
    try:
        if li.count() > 0:
            li.scroll_into_view_if_needed()
            li.click(timeout=3000, force=True)
            page.wait_for_timeout(500)
            if conteudo_visivel():
                return True
    except Exception:
        pass

    # 2) click no A
    try:
        if a.count() > 0:
            a.scroll_into_view_if_needed()
            a.click(timeout=3000, force=True)
            page.wait_for_timeout(500)
            if conteudo_visivel():
                return True
    except Exception:
        pass

    # 3) JS forte (sequência de eventos)
    try:
        ok = page.evaluate(
            """
            () => {
                const li = document.querySelector("li[data-tab='div_estrutura']");
                const a  = li ? li.querySelector("a") : null;
                const el = a || li;
                if (!el) return false;

                const fire = (type) => el.dispatchEvent(new MouseEvent(type, { bubbles:true, cancelable:true, view:window }));
                fire("mouseover"); fire("mousedown"); fire("mouseup"); fire("click");
                if (li && el !== li) li.dispatchEvent(new MouseEvent("click", { bubbles:true, cancelable:true, view:window }));
                return true;
            }
            """
        )
        if not ok:
            return False

        page.wait_for_function(
            """
            () => {
                const host = document.querySelector("#div_estrutura, div#div_estrutura");
                if (!host) return false;
                const s = getComputedStyle(host);
                return s.display !== "none" && s.visibility !== "hidden" && host.offsetHeight > 0;
            }
            """,
            timeout=timeout_ms,
        )
        return True

    except PlaywrightTimeoutError:
        return False
    except Exception:
        return False


def produto_tem_estrutura(page) -> bool:
    """Detecta rapidamente se existe aba Estrutura com conteúdo"""

    try:
        return page.evaluate("""
            () => {
                const aba = document.querySelector("li[data-tab='div_estrutura']");
                if (!aba) return false;

                aba.click();

                const host = document.querySelector("#div_estrutura");
                if (!host) return false;

                // se tem botão adicionar componente = é kit
                if (host.innerText.includes("Adicionar componente"))
                    return true;

                // se não tem tabela nem mensagem = não é kit
                if (!host.innerText || host.innerText.length < 5)
                    return false;

                return true;
            }
        """)
    except:
        return False

def descobrir_estrutura(sku: str) -> List[Dict]:
    print(f"🤖 Buscando estrutura do SKU {sku}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,                # 👈 invisível
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-notifications",
            ]
        )

        context = browser.new_context(
            storage_state="bling_state.json",
            viewport={"width": 1280, "height": 720}
        )
        page = context.new_page()

        try:
            # abrir produtos
            page.goto(BLING_PRODUTOS_URL, wait_until="domcontentloaded", timeout=120000)
            garantir_logado(page)

            # pesquisar sku
            busca = page.locator("#pesquisa-mini")
            busca.wait_for(state="visible", timeout=60000)
            busca.fill(str(sku))
            busca.press("Enter")
            page.wait_for_timeout(1500)

            # aguarda tabela
            page.wait_for_selector("table.tabela-listagem tbody tr", timeout=60000)
            linhas = page.locator("table.tabela-listagem tbody tr")

            if linhas.count() == 0:
                print("❌ Produto não encontrado")
                return []

            # tenta abrir o produto pela linha
            primeira_linha = linhas.first
            ok = _abrir_produto_pela_linha(page, primeira_linha, sku)

            if not ok:
                print("❌ Não consegui abrir o produto a partir da linha (sem navegação)")
                return []

            # agora estamos na tela do produto
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(800)

            # DEBUG útil
            try:
                print("DEBUG: url atual:", page.url)
                print("DEBUG: aba estrutura count:", page.locator("li[data-tab='div_estrutura']").count())
            except Exception:
                pass

            # clicar aba estrutura (robusto)
            ok_tab = abrir_aba_estrutura(page, timeout_ms=5000)

            if not ok_tab:
                print("⚠️ Produto sem aba estrutura")
                return []

            # 🔥 NOVO: verificação rápida
            if not produto_tem_estrutura(page):
                print("⚠️ Produto não possui estrutura cadastrada no Bling")
                return []

            # ✅ esperar a tabela da estrutura aparecer (layout atual = TDs)
            # primeiro tenta o seletor antigo (alguns layouts ainda usam)
            try:
                page.wait_for_selector("tbody.itens-list tr", timeout=8000)
            except PlaywrightTimeoutError:
                # fallback: qualquer tbody com linhas (a estrutura aparece como grid)
                page.wait_for_selector("table tbody tr", timeout=5000)

            # ✅ agora extrair pelos TDs (como na sua imagem)
            componentes: List[Dict] = []

            rows = page.locator("tr[id^='detailItem']")

            print("DEBUG: linhas reais:", rows.count())

            for i in range(rows.count()):
                row = rows.nth(i)

                try:
                    nome = row.locator("input[name='produto']").input_value().strip()
                    sku_filho = row.locator("input[name='codigoItemEstrutura']").input_value().strip()
                    qtd_raw = row.locator("input[name='quantidade']").input_value().strip()

                    qtd = float(qtd_raw.replace(".", "").replace(",", "."))

                    componentes.append({
                        "sku": sku_filho,
                        "nome": nome,
                        "quantidade": qtd
                    })

                except Exception:
                    pass

            if not componentes:
                print("⚠️ Cliquei em Estrutura, mas não encontrei linhas de componentes")
                return []

            print(f"📦 Estrutura encontrada: {len(componentes)} itens")
            return componentes

        finally:
            try:
                browser.close()
            except Exception:
                pass