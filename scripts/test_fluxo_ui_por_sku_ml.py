import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BLING_VENDAS_URL = "https://www.bling.com.br/vendas.php#list"
BLING_NOTAS_URL = "https://www.bling.com.br/notas.fiscais.php#list"


# =========================
# Helpers EXATOS (anti-erro)
# =========================
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def row_text(row) -> str:
    try:
        return _norm(row.inner_text(timeout=1500))
    except Exception:
        return ""


def find_row_exact_token(page, token: str):
    """
    Acha a row que contém o token como número EXATO (não parte de outro número).
    Ex: token=40255 NÃO bate em 140255
    """
    token = str(token).strip()
    rows = page.locator("table.tabela-listagem tbody tr")
    n = rows.count()
    pat = re.compile(rf"(?<!\d){re.escape(token)}(?!\d)")
    for i in range(n):
        r = rows.nth(i)
        txt = row_text(r)
        if txt and pat.search(txt):
            return r
    return None


def mark_row_checkbox(row):
    """
    Marca checkbox da linha (tentando input direto e fallback).
    """
    row.wait_for(state="visible", timeout=30000)

    # tenta input primeiro (mais confiável)
    inp = row.locator('td.checkbox-item input[type="checkbox"]').first
    if inp.count() > 0:
        try:
            inp.check(force=True)
            return True
        except Exception:
            pass

    # fallback: label/div
    cb = row.locator("td.checkbox-item .input-checkbox, td.checkbox-item label").first
    if cb.count() > 0:
        cb.scroll_into_view_if_needed()
        cb.click(force=True)
        return True

    raise Exception("❌ Não achei checkbox na linha.")


def buscar_no_mini(page, texto: str):
    """Usa o input #pesquisa-mini + Enter e espera a tabela."""
    texto = str(texto or "").strip()
    if not texto:
        raise Exception("buscar_no_mini: texto vazio")

    inp = page.locator("#pesquisa-mini").first
    inp.wait_for(state="visible", timeout=60000)
    inp.click()
    inp.fill("")
    inp.type(texto, delay=30)
    inp.press("Enter")

    # espera atualizar resultados
    page.wait_for_timeout(600)
    page.wait_for_selector("table.tabela-listagem tbody tr", timeout=60000)


def extrair_numero_nf_do_link(nfe_link) -> str:
    """
    Extrai o número visível da NF a partir do texto do link (preferível)
    ou do href como fallback.
    Aceita 5+ dígitos (ex: 075465).
    """
    txt = ""
    href = ""
    try:
        txt = (nfe_link.inner_text() or "").strip()
    except Exception:
        txt = ""
    try:
        href = (nfe_link.get_attribute("href") or "").strip()
    except Exception:
        href = ""

    # procura um número de NF no texto do link primeiro
    m = re.search(r"(?<!\d)(\d{5,})(?!\d)", txt)
    if not m:
        # fallback: procura no href
        m = re.search(r"(?<!\d)(\d{5,})(?!\d)", href)

    if not m:
        raise Exception(f"❌ Não consegui extrair numero_nf. txt='{txt}' href='{href}'")

    return m.group(1)


class BlingRPA:
    def __init__(self, headless=True):
        self.headless = headless

    def emitir(self, numero_pedido: str):
        """
        Abre o Bling, busca pedido pelo número visível (ex: 40255) e emite NF-e
        + baixa DANFE.
        """
        numero_pedido = str(numero_pedido).strip()
        if not numero_pedido:
            raise Exception("numero_pedido vazio")

        print("🤖 Iniciando navegador...")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--start-maximized"]
            )

            context = browser.new_context(
                storage_state="bling_state.json",  # sessão já logada
                viewport={"width": 1366, "height": 900},
                accept_downloads=True
            )

            page = context.new_page()

            # --------------------------------------------------
            # ABRE TELA DE VENDAS (LISTA)
            # --------------------------------------------------
            print("🌐 Abrindo página de vendas...")
            page.goto(BLING_VENDAS_URL, wait_until="domcontentloaded")
            page.wait_for_selector("#pesquisa-mini", timeout=60000)

            # --------------------------------------------------
            # PESQUISA PEDIDO (número visível no Bling, ex: 40255)
            # --------------------------------------------------
            print(f"🔎 Buscando pedido {numero_pedido} ...")
            buscar_no_mini(page, numero_pedido)

            # garante que existe linha EXATA (pra não marcar errado)
            row_pedido = find_row_exact_token(page, numero_pedido)
            if not row_pedido:
                raise Exception(f"❌ Não achei a linha EXATA do pedido {numero_pedido} na lista (filtro falhou).")

            print("✅ Pedido apareceu na lista (linha EXATA).")

            # --------------------------------------------------
            # MARCAR O PEDIDO (EXATO)
            # --------------------------------------------------
            print("☑️ Marcando o pedido EXATO...")
            mark_row_checkbox(row_pedido)
            print("✅ Marcou o pedido (linha EXATA).")

            # --------------------------------------------------
            # GERAR NF-e (mantive teu bloco comentado intacto)
            # --------------------------------------------------
            # ... teu código comentado continua igual ...

            # --------------------------------------------------
            # DESCOBRIR NÚMERO DA NF (correto)
            # --------------------------------------------------
            print("🔎 Descobrindo número da NF gerada...")

            # botão/link azul da NF / DANFE na linha do pedido
            nfe_link = row_pedido.locator("a[href*='notas.fiscais.php'], a[href*='nota_fiscal']").first
            nfe_link.wait_for(state="visible", timeout=30000)

            href = nfe_link.get_attribute("href")
            print("href NF:", href)

            numero_nf = extrair_numero_nf_do_link(nfe_link)
            print("✅ numero_nf detectado:", numero_nf)

            # --------------------------------------------------
            # IR PRA NOTAS FISCAIS E IMPRIMIR DANFE SIMPLIFICADO + ETIQUETA
            # --------------------------------------------------
            print("🌐 Indo para lista de notas fiscais...")
            page.goto(BLING_NOTAS_URL, wait_until="domcontentloaded")

            # espera a página de notas carregar (tabela + busca)
            page.wait_for_selector("#pesquisa-mini", timeout=60000)
            page.wait_for_selector("table.tabela-listagem tbody", timeout=60000)

            print(f"🔎 Procurando NF {numero_nf} ...")
            buscar_no_mini(page, numero_nf)

            # agora a row tem que ser achada pela NF (EXATO)
            row_nf = find_row_exact_token(page, numero_nf)
            if not row_nf:
                # fallback: tenta sem zeros à esquerda
                if numero_nf.isdigit():
                    alt = str(int(numero_nf))
                    if alt != numero_nf:
                        print(f"⚠️ Não achei com '{numero_nf}', tentando '{alt}' ...")
                        buscar_no_mini(page, alt)
                        row_nf = find_row_exact_token(page, alt)

            if not row_nf:
                raise Exception(f"❌ Não achei linha EXATA da NF {numero_nf} na lista de notas.")

            print("☑️ Marcando NF-e (linha EXATA)...")
            mark_row_checkbox(row_nf)
            print("✅ NF marcada (linha EXATA).")

            # menu lateral já aparece, mas espera o item existir
            item_print = page.locator("li:has-text('DANFE Simplificado + Etiqueta de transporte')").first
            item_print.wait_for(state="visible", timeout=30000)

            print("🖨️ Gerando DANFE Simplificado + Etiqueta...")

            with page.expect_download(timeout=120000) as download_info:
                item_print.click()

            download = download_info.value
            out = f"danfe_{numero_pedido}_nf_{numero_nf}.pdf"
            download.save_as(out)

            print(f"📄 DANFE salvo! -> {out}")

            browser.close()
            return out