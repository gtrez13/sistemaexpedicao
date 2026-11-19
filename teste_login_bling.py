"""Testa o login do robô no Bling de 3 jeitos e grava a sessão (bling_state.json) no primeiro que entrar.

Uso (na pasta do WMS):  venv/bin/python -u teste_login_bling.py
Para no primeiro que der certo (no máximo 3 tentativas, com pausa, para não bloquear o usuário).
Mostra também a resposta do servidor do Bling ao login (status e trecho), sem a senha.
"""
import os
import re
import sys
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
USER, PWD = os.getenv("BLING_USER"), os.getenv("BLING_PASS")
if not USER or not PWD:
    sys.exit("BLING_USER / BLING_PASS não estão no .env")
URL = "https://www.bling.com.br/login"
ESTADO = os.path.join(BASE, "bling_state.json")
FOTOS = os.path.join(BASE, "downloads")
os.makedirs(FOTOS, exist_ok=True)
UA_FAKE = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def tentar(p, nome, ua_fake: bool, enviar: str, headless_novo: bool):
    print(f"\n=== {nome} ===", flush=True)
    kw = dict(headless=True, args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
    if headless_novo:
        kw["channel"] = "chromium"          # headless "novo" (Chrome de verdade), playwright >= 1.49
    try:
        browser = p.chromium.launch(**kw)
    except Exception as e:
        print(f"  não abriu o navegador: {str(e)[:200]}")
        return False
    ctx_kw = dict(locale="pt-BR", timezone_id="America/Sao_Paulo", viewport={"width": 1440, "height": 900})
    if ua_fake:
        ctx_kw["user_agent"] = UA_FAKE
    else:  # UA real do navegador, só sem o "Headless"
        b = browser.new_context(); pg = b.new_page()
        ctx_kw["user_agent"] = pg.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        b.close()
    print(f"  UA: {ctx_kw['user_agent']}")
    ctx = browser.new_context(**ctx_kw)
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = ctx.new_page()

    respostas = []
    def on_resp(r):
        u = r.url
        if r.request.method == "POST" and re.search(r"login|auth|oauth|token|session|captcha", u, re.I):
            try:
                corpo = r.text()[:300]
            except Exception:
                corpo = ""
            corpo = corpo.replace(PWD, "***")
            respostas.append(f"{r.status} {u[:120]} -> {re.sub(r'\s+', ' ', corpo)}")
    page.on("response", on_resp)

    ok = False
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("#username", state="visible", timeout=30000)
        page.wait_for_timeout(2000)
        usu = page.locator("#username").first
        sen = page.locator("#login-password, input[type='password']").first
        usu.click(); usu.press_sequentially(USER, delay=60)
        page.wait_for_timeout(500)
        sen.click(); sen.press_sequentially(PWD, delay=60)
        page.wait_for_timeout(800)
        print(f"  campos: usuário ok={usu.input_value() == USER}, senha {len(sen.input_value())}/{len(PWD)} caracteres")
        if enviar == "botao":
            page.locator("form#login button[type=submit], button:has-text('Entrar')").first.click(timeout=15000)
        else:
            sen.press("Enter")
        try:
            page.wait_for_function("() => !location.href.includes('login') || /incorret|inv[aá]lid|bloquead|captcha|verifica/i.test(document.body.innerText)", timeout=60000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        ok = "login" not in page.url
        txt = re.sub(r"\s+", " ", page.inner_text("body", timeout=5000))[:200]
        print(f"  URL: {page.url[:100]}")
        print(f"  tela: {txt}")
        foto = os.path.join(FOTOS, f"teste_login_{nome.split()[0]}.png")
        page.screenshot(path=foto); print(f"  foto: {foto}")
    except Exception as e:
        print(f"  erro: {type(e).__name__}: {str(e)[:200]}")
    for r in respostas:
        print(f"  servidor: {r}")
    if ok:
        ctx.storage_state(path=ESTADO)
        print(f"  ✅ ENTROU. Sessão gravada em {ESTADO}")
    else:
        print("  ❌ não entrou")
    browser.close()
    return ok


with sync_playwright() as p:
    jeitos = [
        ("1 atual (UA fixo Chrome/131 + Enter)", True, "enter", False),
        ("2 UA real + botão Entrar", False, "botao", False),
        ("3 headless novo + UA real + botão", False, "botao", True),
    ]
    for i, (nome, ua_fake, enviar, novo) in enumerate(jeitos):
        if i:
            time.sleep(20)
        if tentar(p, nome, ua_fake, enviar, novo):
            print(f"\nFuncionou no jeito {nome}. Clique em Continuar a entrada no WMS.")
            break
    else:
        print("\nNenhum dos 3 entrou. Me mande esta saída inteira.")
