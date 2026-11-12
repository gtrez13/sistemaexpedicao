# services/bling_entrada_robo.py
"""Robô (tela do Bling) SÓ para o que a API do Bling não faz: MANIFESTAR e IMPORTAR a nota do fornecedor.
Todo o resto da entrada (vincular ao pedido de compra, pedido Atendido, estoque, lotes, contas) é pela API, no WMS.

Caminho mapeado em 12/11/2026 (NF 204203):
  Estoque > Notas fiscais de entrada (notas.entrada.php) > aba "Notas recebidas"
  linha da nota: <select id="operacao{ID}" class="notas-recebidas-select"> (0 Não informada, 210210 Ciente,
  210200 Confirmo a operação, ...) + <input id="btn_manifestar{ID}"> -> modal "Nota(s) manifestada(s) com sucesso!"
  depois a linha fica "Op realizada" com <span id="btn_editar{ID}"> (Editar NF-e) que abre outra aba
  notas.entrada.php#edit/{id da nota}: formulário já preenchido pelo XML -> "Salvar" importa (vira "Registrada" em
  Notas de compra, SEM lançar estoque nem contas).
Fotos de cada passo em downloads/robo_entrada/ (para ver onde parou).

Teste à mão:  venv/bin/python -u -m services.bling_entrada_robo <nº da NF> [chave]"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

URL_ENTRADAS = os.environ.get("BLING_ENTRADAS_URL", "https://www.bling.com.br/notas.entrada.php")
EVENTO_CONFIRMO = "210200"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOTOS = os.path.join(BASE_DIR, "downloads", "robo_entrada")
_lock = threading.Lock()          # um robô por vez


class RoboErro(Exception):
    pass


def _foto(page, nome):
    try:
        os.makedirs(FOTOS, exist_ok=True)
        cam = os.path.join(FOTOS, f"{time.strftime('%Y%m%d_%H%M%S')}_{nome}.png")
        page.screenshot(path=cam, full_page=False)
        return cam
    except Exception:
        return None


def _dig(v):
    return re.sub(r"\D", "", str(v or ""))


def manifestar_e_importar(numero: str, chave: str = "", valor: float | None = None, evento: str = EVENTO_CONFIRMO,
                          headless: bool = True, log=print) -> dict:
    """-> {"ok": True, "nf_id": "...", "passos": [...]} ou levanta RoboErro com a mensagem e a foto."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from services.bling_nfe_rpa import BlingRPA

    numero = str(numero).lstrip("0") or str(numero)
    passos = []

    def passo(t):
        passos.append(t)
        log(f"[ROBO ENTRADA] NF {numero}: {t}")

    if not _lock.acquire(timeout=600):
        raise RoboErro("outro robô do Bling está rodando há mais de 10 min")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            estado = os.path.join(BASE_DIR, "bling_state.json")
            context = browser.new_context(storage_state=estado if os.path.exists(estado) else None,
                                          viewport={"width": 1440, "height": 900})
            page = context.new_page()
            rpa = BlingRPA(headless=headless)
            try:
                page.goto(URL_ENTRADAS, wait_until="domcontentloaded", timeout=120000)
                rpa.garantir_logado(page)
                if "notas.entrada" not in page.url:
                    page.goto(URL_ENTRADAS, wait_until="domcontentloaded", timeout=120000)
                rpa.wait_no_overlay(page, 60000)
                passo("entrou no Bling (Notas fiscais de entrada)")

                # aba Notas recebidas
                page.get_by_text("Notas recebidas", exact=True).first.click(timeout=30000)
                page.wait_for_timeout(1500)
                rpa.wait_no_overlay(page, 60000)
                # situação: Todas (a nota pode já ter sido manifestada e sair de "Com pendência")
                filtro = page.locator("select:has(option[value='comPend'])").first
                try:
                    filtro.select_option(index=0, timeout=10000)          # "Todas"
                    page.get_by_text("Filtrar", exact=True).first.click(timeout=10000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                busca = page.locator("input[placeholder*='nº da nota'], input[placeholder*='Pesquisar']").first
                busca.fill(numero, timeout=20000)
                busca.press("Enter")
                page.wait_for_timeout(2500)
                rpa.wait_no_overlay(page, 60000)

                linhas = page.locator("tr", has=page.locator("select.notas-recebidas-select"))
                alvo = None
                for k in range(linhas.count()):
                    tr = linhas.nth(k)
                    txt = tr.inner_text(timeout=5000)
                    cols = re.findall(r"\S+", txt)
                    if numero not in cols:
                        continue
                    if valor is not None:
                        v = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                        if v not in txt:
                            continue
                    alvo = tr
                    break
                if alvo is None:
                    raise RoboErro(f"não achei a NF {numero} em Notas recebidas (foto: {_foto(page, f'{numero}_nao_achou')})")
                sel = alvo.locator("select.notas-recebidas-select").first
                sid = (sel.get_attribute("id") or "").replace("operacao", "")
                atual = sel.input_value()
                passo(f"achei a nota em Notas recebidas (id interno {sid}, manifestação atual {atual})")

                # manifestar (Confirmo a operação)
                if atual in ("0", "", "210210"):
                    sel.select_option(evento)
                    page.locator(f"#btn_manifestar{sid}").click(timeout=15000)
                    try:
                        page.get_by_text("manifestada(s) com sucesso", exact=False).first.wait_for(timeout=90000)
                    except PWTimeout:
                        raise RoboErro(f"o Bling não confirmou a manifestação (foto: {_foto(page, f'{numero}_manifestar')})")
                    passo("manifestada: Confirmo a operação")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(800)
                    try:
                        page.locator(".ui-dialog-titlebar-close, button:has-text('×')").first.click(timeout=3000)
                    except Exception:
                        pass
                    # relista para a linha mostrar "Op realizada" com o botão de editar
                    busca.fill(numero)
                    busca.press("Enter")
                    page.wait_for_timeout(2500)
                elif atual in ("210220", "210240", "1"):
                    raise RoboErro(f"a nota está como '{atual}' (desconhecida / não realizada / ignorada) no Bling: não importo")
                else:
                    passo("já estava manifestada")

                # importar: Editar NF-e abre o formulário pronto em outra aba -> Salvar
                bot = page.locator(f"#btn_editar{sid}")
                try:
                    bot.wait_for(state="attached", timeout=30000)
                except PWTimeout:
                    raise RoboErro(f"depois de manifestar não apareceu o botão de importar (foto: {_foto(page, f'{numero}_sem_editar')})")
                with context.expect_page(timeout=60000) as nova:
                    bot.click(timeout=15000, force=True)
                form = nova.value
                form.wait_for_load_state("domcontentloaded", timeout=120000)
                form.wait_for_timeout(2500)
                rpa.wait_no_overlay(form, 60000)
                m = re.search(r"#edit/(\d+)", form.url)
                nf_id = m.group(1) if m else ""
                salvar = form.get_by_role("button", name="Salvar").first
                salvar.wait_for(timeout=60000)
                # confere que o formulário é dessa nota (número)
                try:
                    valores = form.locator("input").evaluate_all("els => els.map(e => (e.value || '').trim())")
                except Exception:
                    valores = None
                if valores is not None and numero not in [v.lstrip("0") for v in valores]:
                    raise RoboErro(f"o formulário aberto não é da NF {numero} (foto: {_foto(form, f'{numero}_form_errado')})")
                _foto(form, f"{numero}_form")
                salvar.click(timeout=15000)
                try:
                    form.wait_for_function("() => location.hash.indexOf('#edit') === -1", timeout=90000)
                except PWTimeout:
                    raise RoboErro(f"o Bling não salvou a nota (foto: {_foto(form, f'{numero}_salvar')})")
                passo(f"importada: Salvar (nota {nf_id or '?'} agora em Notas de compra)")
                try:
                    context.storage_state(path=estado)
                except Exception:
                    pass
                return {"ok": True, "nf_id": nf_id, "passos": passos}
            except RoboErro:
                raise
            except Exception as e:
                raise RoboErro(f"{type(e).__name__}: {str(e)[:300]} (foto: {_foto(page, f'{numero}_erro')})")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    finally:
        _lock.release()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv; load_dotenv(".env")
    except Exception:
        pass
    if len(sys.argv) < 2:
        sys.exit("uso: venv/bin/python -u -m services.bling_entrada_robo <nº da NF> [chave]")
    try:
        print(manifestar_e_importar(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
    except RoboErro as e:
        sys.exit(f"ROBÔ PAROU: {e}")
