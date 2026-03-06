from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://www.bling.com.br")

    input("FAZ LOGIN E APERTA ENTER")

    context.storage_state(path="bling_state.json")
    browser.close()