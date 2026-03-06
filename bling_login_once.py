from playwright.sync_api import sync_playwright

LOGIN_URL = "https://www.bling.com.br/login"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()

    page = context.new_page()
    page.goto(LOGIN_URL)

    print("\n➡ Faça login manualmente no Bling.")
    print("➡ Depois volte aqui no terminal e pressione ENTER.\n")
    input()

    context.storage_state(path="bling_state.json")
    browser.close()

print("✅ Sessão salva em bling_state.json")