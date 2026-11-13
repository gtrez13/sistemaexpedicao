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

Teste à mão:  venv/bin/python -u -m services.bling_entrada_robo <nº da NF> [chave]
              venv/bin/python -u -m services.bling_entrada_robo conciliar <nº da NF> <nº do pedido de compra>"""
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


def _brl_num(v):
    try:
        return float(str(v or "0").replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


def conciliar_pedido(numero: str, pc_numero: str, valor: float | None = None, n_itens: int | None = None,
                     headless: bool = True, log=print) -> dict:
    """CONCILIA a nota de entrada (já importada) com o pedido de compra, item a item, NA TELA do Bling.
    A API v3 não grava esse vínculo (PUT itens[].notaFiscal volta 200 e não salva, testado 12/11) e o Bling TRAVA a conciliação
    depois que a nota lança estoque ou contas ("Conciliação bloqueada devido a lançamentos realizados previamente"):
    por isso roda antes do estoque.
    Caminho (mapeado 13/11 na NF 204203):
      Notas de compra > marcar a nota (#marcadodatatable{id}) > menu "Vincular à pedido de compra" (li#vincularPedidoCompra)
      > tela "Conciliação dos itens": por item (tr.linha-conciliacao) botão "Pedido de compra" > painel #slideIn-pedido-compra
      > "Conciliar pedido" > campo purchaseOrderNumberSearch (o Bling SUGERE o pedido mais antigo com saldo, que pode ser outro:
      sempre digita o nosso) > item "{pc} | data" > Salvar (#btn-salvar-pedido-compra)
      > "Conciliar Produtos" (#botaoSalvarConciliacao) > Salvar a nota (#botaoSalvar).
    -> {"ok": True, "itens": n, "passos": [...]} ou RoboErro (mensagem começa com TRAVADA: quando o Bling não deixa mais)."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from services.bling_nfe_rpa import BlingRPA

    numero = str(numero).lstrip("0") or str(numero)
    pc_numero = str(pc_numero).strip()
    passos = []

    def passo(t):
        passos.append(t)
        log(f"[ROBO CONCILIAR] NF {numero} x pedido {pc_numero}: {t}")

    if not _lock.acquire(timeout=600):
        raise RoboErro("outro robô do Bling está rodando há mais de 10 min")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            estado = os.path.join(BASE_DIR, "bling_state.json")
            context = browser.new_context(storage_state=estado if os.path.exists(estado) else None,
                                          viewport={"width": 1600, "height": 900})
            page = context.new_page()
            rpa = BlingRPA(headless=headless)
            try:
                page.goto(URL_ENTRADAS, wait_until="domcontentloaded", timeout=120000)
                rpa.garantir_logado(page)
                if "notas.entrada" not in page.url:
                    page.goto(URL_ENTRADAS, wait_until="domcontentloaded", timeout=120000)
                rpa.wait_no_overlay(page, 60000)
                page.locator("li.tab-notasCompra a").first.click(timeout=30000)      # aba "Notas de compra"
                page.wait_for_timeout(1200)
                rpa.wait_no_overlay(page, 60000)
                busca = page.locator("input[placeholder*='da nota']").first
                busca.fill(numero, timeout=20000)
                busca.press("Enter")
                page.wait_for_timeout(2500)
                rpa.wait_no_overlay(page, 60000)

                alvo = None
                for k in range(page.locator(f"tr[numero='{numero}']").count()):
                    tr = page.locator(f"tr[numero='{numero}']").nth(k)
                    if valor is not None and abs(float(tr.get_attribute("valor") or 0) - float(valor)) > 0.05:
                        continue
                    alvo = tr
                    break
                if alvo is None:
                    raise RoboErro(f"não achei a NF {numero} em Notas de compra (foto: {_foto(page, f'{numero}_conc_nao_achou')})")
                nid = alvo.get_attribute("idnota")
                if (alvo.get_attribute("estoque") or "") == "S" or (alvo.get_attribute("contas") or "") == "S":
                    raise RoboErro("TRAVADA: a nota já lançou estoque ou contas no Bling e ele não deixa mais conciliar com o pedido")
                passo(f"achei a nota (id {nid}, conciliação atual: {alvo.get_attribute('situacaoconciliacao')})")

                # o checkbox e o item do menu lateral são estilizados: clique por JS (igual a um clique na tela)
                page.evaluate("id => { const c = document.getElementById(id); if (c && !c.checked) c.click() }", f"marcadodatatable{nid}")
                page.wait_for_timeout(800)
                if not page.evaluate("id => !!(document.getElementById(id) || {}).checked", f"marcadodatatable{nid}"):
                    raise RoboErro(f"não consegui marcar a nota na lista (foto: {_foto(page, f'{numero}_conc_marcar')})")
                page.evaluate("() => document.getElementById('vincularPedidoCompra').click()")
                cards = page.locator("tr.linha-conciliacao")
                cards.first.wait_for(state="visible", timeout=90000)
                try:
                    page.get_by_text("Obtendo os dados de conciliação", exact=False).first.wait_for(state="hidden", timeout=90000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(1500)
                total = cards.count()
                if n_itens and total != n_itens:
                    raise RoboErro(f"a tela de conciliação mostrou {total} itens e a nota tem {n_itens} (foto: {_foto(page, f'{numero}_conc_itens')})")
                passo(f"tela de conciliação aberta ({total} itens)")

                painel = page.locator("#slideIn-pedido-compra")
                for k in range(total):
                    card = cards.nth(k)
                    card.scroll_into_view_if_needed(timeout=10000)
                    card.locator("button", has_text="Pedido de compra").first.click(timeout=15000)
                    painel.wait_for(state="visible", timeout=30000)
                    page.wait_for_timeout(800)
                    painel.locator("label.Checkbox-card", has_text="Conciliar pedido").first.click(timeout=15000)
                    campo = painel.locator("input[name='purchaseOrderNumberSearch']").first
                    campo.wait_for(state="visible", timeout=30000)
                    page.wait_for_timeout(1200)
                    campo.click()
                    campo.press("Control+A")
                    campo.type(pc_numero, delay=80)
                    opc = page.locator("ul.Dropdown li.Dropdown-item", has_text=f"{pc_numero} |").first
                    try:
                        opc.wait_for(state="visible", timeout=30000)
                    except PWTimeout:
                        raise RoboErro(f"item {k + 1}: o pedido {pc_numero} não aparece para esse produto (sem saldo ou outro fornecedor) "
                                       f"(foto: {_foto(page, f'{numero}_conc_item{k + 1}')})")
                    opc.click(timeout=10000)
                    page.wait_for_timeout(1200)
                    escolhido = campo.input_value()
                    saldo = painel.locator("input[id^='purchaseOrderItemLastBalance_']").first.input_value()
                    if not escolhido.startswith(f"{pc_numero} |") or _brl_num(saldo) <= 0:
                        raise RoboErro(f"item {k + 1}: não ficou o pedido {pc_numero} com saldo (campo '{escolhido}', saldo {saldo}) "
                                       f"(foto: {_foto(page, f'{numero}_conc_item{k + 1}')})")
                    page.locator("#btn-salvar-pedido-compra").click(timeout=15000)
                    painel.wait_for(state="hidden", timeout=30000)
                    page.wait_for_timeout(600)
                    passo(f"item {k + 1}: pedido {pc_numero} (saldo {saldo})")

                txt = page.locator("#tabela-conciliacao").inner_text(timeout=10000) if page.locator("#tabela-conciliacao").count() else page.inner_text("body")
                nao = txt.count("Pedido não conciliado")
                if nao:
                    raise RoboErro(f"{nao} item(ns) ficaram sem conciliar (foto: {_foto(page, f'{numero}_conc_faltou')})")
                botao = page.locator("#botaoSalvarConciliacao")
                if botao.is_disabled():
                    raise RoboErro("TRAVADA: o Bling não liberou 'Conciliar Produtos' (nota com lançamentos)")
                _foto(page, f"{numero}_conc_pronta")
                botao.click(timeout=15000)
                salvar = page.locator("#botaoSalvar")
                salvar.wait_for(state="visible", timeout=60000)
                page.wait_for_timeout(1500)
                salvar.click(timeout=15000)
                try:
                    page.wait_for_function("() => location.hash.indexOf('#edit') === -1", timeout=90000)
                except PWTimeout:
                    raise RoboErro(f"o Bling não salvou a nota depois de conciliar (foto: {_foto(page, f'{numero}_conc_salvar')})")
                passo("conciliada e nota salva")

                # confere na lista: a coluna Vinculação da nota
                page.wait_for_timeout(2000)
                busca = page.locator("input[placeholder*='da nota']").first
                busca.fill(numero)
                busca.press("Enter")
                page.wait_for_timeout(2500)
                sit = page.locator(f"tr[idnota='{nid}']").first.get_attribute("situacaoconciliacao") if page.locator(f"tr[idnota='{nid}']").count() else None
                passo(f"conciliação no Bling agora: {sit}")
                try:
                    context.storage_state(path=estado)
                except Exception:
                    pass
                return {"ok": True, "itens": total, "situacao": sit, "passos": passos}
            except RoboErro:
                raise
            except Exception as e:
                raise RoboErro(f"{type(e).__name__}: {str(e)[:300]} (foto: {_foto(page, f'{numero}_conc_erro')})")
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
        sys.exit("uso: venv/bin/python -u -m services.bling_entrada_robo <nº da NF> [chave]  |  conciliar <nº da NF> <nº do pedido>")
    try:
        if sys.argv[1] == "conciliar":
            print(conciliar_pedido(sys.argv[2], sys.argv[3]))
        else:
            print(manifestar_e_importar(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
    except RoboErro as e:
        sys.exit(f"ROBÔ PAROU: {e}")
