import re
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

VENDAS_URL = "https://www.bling.com.br/vendas.php#list"
NOTAS_URL  = "https://www.bling.com.br/notas.fiscais.php#list"


def wait_no_overlay(page, timeout=180000):
    try:
        page.wait_for_function("() => !document.querySelector('.blockUI.blockOverlay')", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def buscar_no_mini(page, texto: str):
    box = page.locator("#pesquisa-mini").first
    box.wait_for(state="visible", timeout=60000)
    box.click()
    box.fill("")
    box.type(str(texto), delay=40)
    box.press("Enter")
    page.wait_for_timeout(800)
    page.wait_for_selector("table.tabela-listagem tbody tr", timeout=60000)


def marcar_primeira_linha(page):
    row = page.locator("table.tabela-listagem tbody tr").first
    row.wait_for(state="visible", timeout=60000)

    inp = row.locator("td.checkbox-item input[type='checkbox']").first
    if inp.count() > 0:
        inp.check(force=True)
        for _ in range(40):
            try:
                if inp.is_checked():
                    return row
            except Exception:
                pass
            page.wait_for_timeout(100)
        raise Exception("❌ Checkbox não ficou checked")

    # fallback visual
    cb = row.locator("td.checkbox-item .input-checkbox, td.checkbox-item label").first
    cb.scroll_into_view_if_needed()
    cb.click(force=True)
    return row


def extrair_id_da_url(href: str) -> str | None:
    """
    Tenta extrair algum id importante do href do Bling.
    A URL pode vir com vários nomes de parâmetro dependendo do módulo.
    """
    if not href:
        return None

    # às vezes vem relativo
    # ex: /notas.fiscais.php?something=...&idNota=123
    u = urlparse(href if "://" in href else "https://www.bling.com.br" + href)
    qs = parse_qs(u.query)

    # tenta chaves comuns
    for key in ("id", "idNota", "idNfe", "idNF", "idNotaFiscal", "id_nota", "idnota"):
        if key in qs and qs[key]:
            return qs[key][0]

    # fallback: pega primeiro número grande na URL
    m = re.search(r"(\d{6,})", href)
    return m.group(1) if m else None


def descobrir_id_nota_pelo_pedido(page, numero_pedido: str) -> str:
    """
    Na tela de vendas, acha a linha do pedido e pega o link azul (NF) pra extrair o ID.
    """
    buscar_no_mini(page, numero_pedido)

    # tenta linha com o número do pedido
    try:
        page.wait_for_selector(
            f'table.tabela-listagem tbody tr:has-text("{numero_pedido}")',
            timeout=15000
        )
    except PlaywrightTimeoutError:
        pass

    row = page.locator(f'table.tabela-listagem tbody tr:has-text("{numero_pedido}")').first
    if row.count() == 0:
        # fallback: primeira linha filtrada
        row = page.locator("table.tabela-listagem tbody tr").first
    row.wait_for(state="visible", timeout=60000)

    # link azul normalmente aponta pra notas.fiscais.php ou algo de nota
    link = row.locator("a[href*='notas.fiscais.php'], a[href*='nota']").first
    link.wait_for(state="visible", timeout=60000)

    href = link.get_attribute("href")
    print("🔗 href NF =", href)

    nota_id = extrair_id_da_url(href or "")
    if not nota_id:
        raise Exception("❌ Não consegui extrair ID da nota pelo href. Cola o href aqui que eu ajusto.")

    print("🧾 ID extraído da nota =", nota_id)
    return nota_id


def abrir_painel_enviar(page):
    btn = page.locator('button[onclick*="enviarNFesSelecionadas"]').first
    btn.wait_for(state="visible", timeout=60000)

    page.wait_for_function("""
    () => {
      const b = document.querySelector('button[onclick*="enviarNFesSelecionadas"]');
      return b && !b.disabled;
    }
    """, timeout=60000)

    btn.click(delay=120)
    page.locator("text=Nota fiscal eletrônica").first.wait_for(state="visible", timeout=60000)


def clicar_enviar_selecionadas_no_painel(page):
    # força tablet pra aparecer o botão
    page.set_viewport_size({"width": 747, "height": 900})
    page.wait_for_timeout(300)

    btn = page.locator("#notaAcao").first
    btn.wait_for(state="visible", timeout=60000)
    btn.scroll_into_view_if_needed()
    btn.click(delay=150, force=True)

    # volta viewport
    page.set_viewport_size({"width": 1366, "height": 900})
    page.wait_for_timeout(250)


def abrir_mais_acoes(page):
    txt = page.locator("text=Mais ações").first
    if txt.count() > 0 and txt.is_visible():
        txt.click()
        return
    page.locator("span.fas.open-more-actions, span.open-more-actions").first.click(force=True)


def baixar_danfe_etiqueta(page, out_name: str):
    item = page.locator("li:has-text('DANFE Simplificado + Etiqueta de transporte')").first
    item.wait_for(state="visible", timeout=60000)

    with page.expect_download(timeout=120000) as d:
        item.click()

    d.value.save_as(out_name)
    return out_name


def imprimir_por_pedido(numero_pedido_visivel: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context = browser.new_context(
            storage_state="bling_state.json",
            viewport={"width": 1366, "height": 900},
            accept_downloads=True
        )
        page = context.new_page()

        # 1) Vendas -> pegar ID da nota pelo href
        print("🌐 Abrindo Vendas...")
        page.goto(VENDAS_URL, wait_until="domcontentloaded")
        page.wait_for_selector("#pesquisa-mini", timeout=60000)

        nota_id = descobrir_id_nota_pelo_pedido(page, numero_pedido_visivel)

        # 2) Notas -> buscar pelo ID extraído
        print("🌐 Abrindo Notas fiscais...")
        page.goto(NOTAS_URL, wait_until="domcontentloaded")
        page.wait_for_selector("#pesquisa-mini", timeout=60000)
        page.wait_for_selector("table.tabela-listagem tbody", timeout=60000)

        print(f"🔎 Buscando NOTA pelo ID {nota_id} ...")
        buscar_no_mini(page, nota_id)

        print("☑️ Marcando nota...")
        marcar_primeira_linha(page)
        print("✅ Nota marcada")

        # 3) Enviar / autorizar
        print("📨 Abrindo painel de envio...")
        abrir_painel_enviar(page)

        print("🟢 Clicando em Enviar selecionadas (#notaAcao)...")
        clicar_enviar_selecionadas_no_painel(page)
        wait_no_overlay(page)
        print("✅ Envio disparado")

        # fecha painel
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # 4) Refiltra e marca de novo (às vezes perde seleção)
        buscar_no_mini(page, nota_id)
        marcar_primeira_linha(page)

        # 5) Imprimir
        print("➕ Mais ações...")
        abrir_mais_acoes(page)

        out = baixar_danfe_etiqueta(page, f"danfe_{numero_pedido_visivel}.pdf")
        print("📄 OK ->", out)

        browser.close()


# TESTE
if __name__ == "__main__":
    imprimir_por_pedido("40255")  # <-- aqui você põe o número visível do pedido no Bling (da tela Vendas)