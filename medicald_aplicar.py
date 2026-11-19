#!/usr/bin/env python3
"""Parte em Python do ligar_medicald.sh (Biel 19/11: "pegar TUDO que fizemos para a LHVMed e jogar pra rodar TAMBÉM na MedicalD").

    aplicar  --alvo PASTA --lhv PASTA_LHVMED --robo ARQ [--orig DIR --registro ARQ] [--faturar]
        Leva para PASTA os módulos da LHVMed (os .py que faltam), o mapa_routes.py novo, o robô da entrada novo,
        registra os blueprints no app.py, ajusta a sessão, aplica as correções da LHVMed no bling_service/ml_service
        (e no routes.py se --faturar) e põe no .env os valores da MedicalD. Com --orig guarda o original de tudo que
        muda e anota em --registro (ADD = arquivo novo, MOD = arquivo mudado) para o desfazer.sh.
        Os arquivos do "miolo" da MedicalD (api_routes.py, routes.py, app.py, services/bling_service.py,
        services/ml_service.py, gera_danfe_zpl.py) NÃO são trocados: recebem só os remendos com âncora.
    testar   --alvo PASTA --lhv PASTA_LHVMED
        Sobe o app da PASTA num processo à parte (sem servir nada), confere se as rotas que as telas novas usam
        existem, se o bling_service/ml_service da MedicalD têm o que os módulos chamam e faz GETs de leitura
        como admin. Sai com 1 se algo grave faltar.
    pip      --lhv FREEZE_LHVMED --med FREEZE_MEDICALD
        Lista os pacotes (nome==versão) que a LHVMed tem e a MedicalD não.

Não imprime senha, token nem endereço de banco.
"""
import argparse
import difflib
import hashlib
import importlib
import json
import os
import re
import shutil
import sys

EXCLUIR_DIRS = {"venv", ".venv", "__pycache__", ".bling_robot_profile", "downloads", "closed", "node_modules", ".git",
                "backups", "logs", "static", "templates", "uploads"}
# miolo da MedicalD: fica o dela (só remendos com âncora)
MIOLO = {"api_routes.py", "routes.py", "app.py", "criar_admin.py", "gera_danfe_zpl.py", "teste_emissao_nfe_api.py",
         "services/bling_service.py", "services/ml_service.py"}
ROBO = "services/bling_entrada_robo.py"
MAPA = "mapa_routes.py"
MAPA_MEDICALD_CONHECIDAS = {"27d607a7eb35"}          # mapa_routes.py da MedicalD = versão de 29/10 do repositório

ENV_MEDICALD = [
    ("BLING_DEPOSITO_ID", "14887785343", "depósito Local da MedicalD no Bling (estoque, Não achado, Full, entrada)"),
    ("BLING_DEPOSITO_LOCAL_ID", "14887785343", ""),
    ("BLING_DEPOSITO_LOCAL", "Local", ""),
    ("BLING_DEPOSITO_DEFEITOS", "Defeitos/Garantia", "nome do depósito de defeitos no Bling da MedicalD"),
    ("EMPRESA_CNPJ", "53697191000159", ""),
    ("EMPRESA_NOME", "Medical Dantas Imp. e Com. de Equip. Médicos e Veterinários Ltda", ""),
    ("DEVOLUCAO_EMPRESA_PADRAO", "MEDICAL", "devoluções: empresa padrão"),
    ("DEVOLUCAO_LOJA_ML", "Medical Dantas", ""),
    ("NAO_ACHADO_ZERAR_BLING", "0", "Não achado NÃO zera o Bling sozinho (ligar só com o OK do Biel)"),
    ("FEFO_ATIVO", "0", "FEFO desligado até cadastrar os lotes da MedicalD"),
    ("RECEB_ETQ_UNICA", "estrito", "entrada: etiqueta única por unidade, conferência só bipando"),
    ("NF_DEV_ENVIAR_AUTO", "0", "nota de devolução não vai sozinha para a SEFAZ (naturezas da MedicalD a configurar)"),
    ("SESSAO_HORAS", "12", ""),
]

METODOS_BLING = ["_download", "_get", "_get_json", "_headers", "_load_tokens", "_post", "_request",
                 "anexar_seriais_em_observacoes", "baixar_danfe_simplificado_por_nfe", "buscar_pedido_venda_id_por_numero_loja",
                 "classificar_erro_emissao_nfe", "emitir_nfe_do_pv", "encontrar_nfe_id_por_pedido_venda",
                 "extrair_nfe_id_do_pedido_venda", "get_pedido_venda_detalhe", "nfe_esta_autorizada", "obter_estoque",
                 "obter_numero_nota", "pedido_venda_exige_serial", "resolver_nfe_por_pv_id",
                 "resolver_pv_e_nfe_por_ml_order_id", "resolver_pv_id_por_ml_id"]
FUNCOES_BLING = ["_is_pdf", "_is_html"]
METODOS_ML = ["_fetch_orders_ready_to_ship", "_request", "baixar_etiqueta_pdf_4x6", "buscar_cards", "obter_pedido",
              "renovar_token", "resolver_ml_para_numero_loja"]

# rotas que as telas novas chamam (tiradas do front; sem Shopee)
PRECISA = [
    "/api/admin/usuarios", "/api/arquivados", "/api/auth/forgot-password", "/api/auth/login", "/api/auth/logout",
    "/api/auth/me", "/api/auth/register-public", "/api/auth/reset-password", "/api/bipar-produto", "/api/comercial/info",
    "/api/comercial/pv/:p", "/api/convites", "/api/convites/:p/cancelar", "/api/convites/validar", "/api/cotar-frete",
    "/api/cubagem/:p", "/api/devolucoes", "/api/devolucoes/:p", "/api/devolucoes/:p/finalizar", "/api/devolucoes/:p/fotos",
    "/api/devolucoes/:p/full", "/api/devolucoes/:p/ml-revisao", "/api/devolucoes/:p/nf-devolucao",
    "/api/devolucoes/:p/nf-devolucao/desfazer", "/api/devolucoes/:p/nf-devolucao/enviar", "/api/devolucoes/:p/preencher",
    "/api/devolucoes/:p/resolver", "/api/devolucoes/:p/sac", "/api/devolucoes/admin", "/api/devolucoes/chegaram",
    "/api/devolucoes/export.csv", "/api/devolucoes/fotos/:p", "/api/devolucoes/full-conferir-todas",
    "/api/devolucoes/importar-ml", "/api/devolucoes/opcoes", "/api/devolucoes/produto", "/api/devolucoes/resumo",
    "/api/devolucoes/venda", "/api/devolucoes/videos/:p", "/api/estoque/atualizar", "/api/estoque/status",
    "/api/etiqueta-ml/:p", "/api/etiqueta-zpl/:p", "/api/expedicao/bipar-lote", "/api/fefo/desfazer", "/api/fefo/liberar",
    "/api/fefo/sku/:p", "/api/fefo/validar", "/api/frete/:p/comprar", "/api/frete/:p/etiqueta", "/api/full/anuncios",
    "/api/full/etiquetas/fila", "/api/full/etiquetas/impressas", "/api/full/importar", "/api/full/kits", "/api/full/novas",
    "/api/full/remessas", "/api/full/remessas/:p", "/api/full/remessas/:p/atualizar", "/api/full/remessas/:p/bipar",
    "/api/full/remessas/:p/dados", "/api/full/remessas/:p/desetiquetar", "/api/full/remessas/:p/desfazer",
    "/api/full/remessas/:p/devolver", "/api/full/remessas/:p/etiquetar", "/api/full/remessas/:p/itens",
    "/api/full/remessas/:p/reimprimir", "/api/full/remessas/:p/status", "/api/full/remessas/:p/validade",
    "/api/full/sugestao/calcular", "/api/inicio/resumo", "/api/inventario/divergencias",
    "/api/inventario/divergencias/:p/decidir", "/api/inventario/gerar", "/api/inventario/mes", "/api/inventario/tarefa/:p",
    "/api/inventario/tarefa/:p/contar", "/api/inventario/tarefa/:p/enviar", "/api/mapa/foto/:p", "/api/mapa/giro",
    "/api/mapa/layout", "/api/mapa/layout/historico", "/api/mapa/layout/voltar", "/api/mapa/localizacao",
    "/api/mapa/localizacoes", "/api/mapa/produto/:p", "/api/mapa/trocar", "/api/metricas", "/api/nao-achado",
    "/api/nao-achado/:p/achei", "/api/nao-achado/:p/decidir", "/api/nao-achado/:p/explicar",
    "/api/nao-achado/:p/reprocessar", "/api/nao-achado/resumo", "/api/painel", "/api/painel-tv", "/api/pedido-resumo/:p",
    "/api/pedido/:p", "/api/pedido/:p/buscar-etiqueta", "/api/pedidos-hoje", "/api/pedidos-status",
    "/api/pedidos/:p/arquivar", "/api/pedidos/:p/concluir", "/api/pedidos/:p/desarquivar",
    "/api/pedidos/:p/frete-concluido", "/api/pedidos/limpar", "/api/pedidos/limpar-separados", "/api/produtos-pendentes",
    "/api/qz-sign", "/api/rastreabilidade/pedido/:p", "/api/rastreabilidade/sku/:p", "/api/rastreabilidade/usuario/:p",
    "/api/recebimento/chave", "/api/recebimento/notas", "/api/recebimento/notas/:p", "/api/recebimento/notas/:p/bipar",
    "/api/recebimento/notas/:p/entrada", "/api/recebimento/notas/:p/entrada/desfazer",
    "/api/recebimento/notas/:p/etiquetas/codigos", "/api/recebimento/notas/:p/etiquetas/desbipar",
    "/api/recebimento/notas/:p/fechar", "/api/recebimento/notas/:p/itens/:p", "/api/recebimento/notas/:p/itens/:p/confirmar",
    "/api/recebimento/notas/:p/kits", "/api/recebimento/notas/:p/pedido", "/api/recebimento/notas/:p/reabrir",
    "/api/recebimento/notas/:p/resolver", "/api/recebimento/produtos", "/api/recebimento/resumo",
    "/api/recebimento/unidade/:p", "/api/recebimento/unidade/ocorrencias", "/api/saida/adiar", "/api/saida/adiar/desfazer",
    "/api/saida/bipar", "/api/saida/desfazer", "/api/saida/fechados-hoje", "/api/saida/historico", "/api/saida/liberar",
    "/api/saida/lista", "/api/separacao/bipar", "/api/separacao/so-hoje", "/api/sync/produtos",
    "/api/sync/vendas-internas", "/api/tarefas", "/api/tarefas/:p/:p", "/api/tarefas/aviso", "/api/tarefas/pessoas",
]
# GETs só de leitura feitos como admin na cópia de teste
LEITURAS = ["/api/auth/me", "/api/pedidos-status", "/api/inicio/resumo", "/api/recebimento/resumo", "/api/recebimento/notas",
            "/api/full/remessas", "/api/tarefas", "/api/mapa/layout", "/api/nao-achado/resumo", "/api/devolucoes/resumo",
            "/api/arquivados", "/api/inventario/divergencias", "/api/convites", "/api/estoque/status"]


def blob(path):
    with open(path, "rb") as f:
        d = f.read()
    return hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest()[:12]


def ler(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def gravar(p, s):
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


class Registro:
    """Guarda o original antes de mudar (MOD) e anota os arquivos novos (ADD). Sem --orig (cópia de teste) não anota nada."""

    def __init__(self, alvo, orig, registro):
        self.alvo, self.orig, self.registro, self.vistos = alvo, orig, registro, set()

    def _anota(self, linha):
        if self.registro:
            with open(self.registro, "a", encoding="utf-8") as f:
                f.write(linha + "\n")

    def mudar(self, rel):
        if not self.orig or rel in self.vistos:
            return
        self.vistos.add(rel)
        src = os.path.join(self.alvo, rel)
        dst = os.path.join(self.orig, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        self._anota(f"MOD {rel}")

    def novo(self, rel):
        if rel in self.vistos:
            return
        self.vistos.add(rel)
        self._anota(f"ADD {rel}")


def arquivos_py(raiz):
    out = []
    for dp, dns, fns in os.walk(raiz):
        rel_dp = os.path.relpath(dp, raiz)
        nivel = 0 if rel_dp == "." else rel_dp.count(os.sep) + 1
        dns[:] = [d for d in dns if d not in EXCLUIR_DIRS and not d.startswith(".") and nivel < 1]
        for fn in fns:
            if fn.endswith(".py"):
                out.append(os.path.normpath(os.path.join(rel_dp, fn)))
    return sorted(out)


# ───────────────────────── remendos com âncora (os mesmos que a LHVMed recebeu) ─────────────────────────
def remendo_app(s, lhv_app):
    """Blueprints que o app.py da LHVMed registra e o da MedicalD não + sessão (ajustar_sessao.sh). -> (texto, avisos)"""
    avisos, fim = [], ("\r\n" if "\r\n" in s else "\n")
    blocos = []
    for m in re.finditer(r"^([ \t]*)from (\w+) import (\w+) as (\w+)\r?\n[ \t]*app\.register_blueprint\(\4[^\n]*\)\r?\n",
                         lhv_app, re.M):
        if m.group(2) not in s:
            blocos.append(m.group(0).replace("\r\n", "\n").replace("\n", fim))
    if blocos:
        anc = re.search(r"^[ \t]*app\.register_blueprint\(mapa_bp[^\n]*\)\r?\n", s, re.M)
        if not anc:
            raise SystemExit("app.py da MedicalD: não achei o registro do mapa_bp para pôr os módulos depois dele (nada mudou)")
        s = s[:anc.end()] + "".join(blocos) + s[anc.end():]
        avisos.append("app.py: registrei " + ", ".join(re.search(r"from (\w+)", b).group(1) for b in blocos))
    if "_sessao_permanente" not in s:
        v1 = "        PERMANENT_SESSION_LIFETIME=timedelta(hours=12)," + fim
        v2 = '    login_manager.session_protection = "strong"' + fim
        if s.count(v1) == 1 and s.count(v2) == 1 and re.search(r"^import os\b|^import .*\bos\b", s, re.M):
            seguro = "        REMEMBER_COOKIE_SECURE=is_prod," + fim if re.search(r"\bis_prod\b", s) else ""
            n1 = ("        # sessão (e \"lembrar de mim\") valem SESSAO_HORAS SEM usar; cada uso renova (ajustar_sessao.sh)" + fim
                  + '        PERMANENT_SESSION_LIFETIME=timedelta(hours=float(os.getenv("SESSAO_HORAS", "12"))),' + fim
                  + "        SESSION_REFRESH_EACH_REQUEST=True," + fim
                  + '        REMEMBER_COOKIE_DURATION=timedelta(hours=float(os.getenv("SESSAO_HORAS", "12"))),' + fim
                  + "        REMEMBER_COOKIE_REFRESH_EACH_REQUEST=True," + fim
                  + "        REMEMBER_COOKIE_HTTPONLY=True," + fim
                  + '        REMEMBER_COOKIE_SAMESITE="Lax",' + fim + seguro)
            n2 = ('    # "strong" deslogava quando o IP mudava (celular Wi-Fi/4G, proxy da Vercel): "basic" só marca como não recente' + fim
                  + '    login_manager.session_protection = "basic"' + fim + fim
                  + "    from flask import session as _sessao" + fim + fim
                  + "    @app.before_request" + fim
                  + "    def _sessao_permanente():" + fim
                  + "        _sessao.permanent = True   # cookie com validade (SESSAO_HORAS), renovada a cada uso" + fim)
            s = s.replace(v1, n1).replace(v2, n2)
            avisos.append("app.py: sessão igual à da LHVMed (não cai quando o IP muda; vale SESSAO_HORAS sem usar)")
        else:
            avisos.append("app.py: o trecho da sessão é diferente do esperado; sessão ficou como estava")
    return s, avisos


def remendo_deposito(s):
    if "# estoque so do deposito local" in s:
        return s, "bling_service: estoque só do depósito local (já estava)"
    anc = ("                if isinstance(depositos, list) and depositos:\n"
           "                    soma = 0\n"
           "                    for d in depositos:")
    if s.count(anc) != 1:
        return None, "bling_service: trecho do estoque por depósito diferente do da LHVMed; ficou como estava"
    novo = ("                if isinstance(depositos, list) and depositos:\n"
            "                    # estoque so do deposito local: ignora FULL ML/B2W/Amazon (podem estar negativos)\n"
            '                    _dep = (os.getenv("BLING_DEPOSITO_ID") or "").strip()\n'
            "                    if _dep:\n"
            '                        depositos = [d for d in depositos if isinstance(d, dict) and str(d.get("id")) == _dep]\n'
            "                    soma = 0\n"
            "                    for d in depositos:")
    return s.replace(anc, novo, 1), "bling_service: estoque só do depósito Local da MedicalD (ignora o FULL negativo)"


def remendo_comercial(s):
    i = s.find("def buscar_vendas_internas")
    if i < 0:
        return None, "bling_service: sem buscar_vendas_internas; Comercial ficou como estava"
    j = s.find("\n    def ", i + 10)
    j = len(s) if j < 0 else j
    f = s[i:j]
    if '"idsSituacoes[]": [15]' in f:
        return s, "bling_service: Comercial só 'Em andamento' (já estava)"
    f2, n = re.subn(r'"idsSituacoes\[\]"\s*:\s*\[[^\]]*\]', '"idsSituacoes[]": [15]', f)
    if n == 0:
        return None, "bling_service: filtro de situação do Comercial diferente; ficou como estava"
    if "COMERCIAL_SO_EM_ANDAMENTO" not in f2:
        m = re.search(r'\n(\s*)if _ml_pat\.match\(numero_loja\):\n\s*continue\n', f2)
        if m:
            ind = m.group(1)
            guard = (f"{ind}# COMERCIAL_SO_EM_ANDAMENTO: garante só 'Em andamento' mesmo se o Bling ignorar o filtro\n"
                     f"{ind}_sit = (pv.get('situacao') or {{}}).get('id')\n"
                     f"{ind}if _sit is not None and str(_sit) != '15':\n{ind}    continue\n")
            f2 = f2[:m.end()] + guard + f2[m.end():]
    return s[:i] + f2 + s[j:], "bling_service: Comercial importa só pedidos 'Em andamento'"


def remendo_variacao(s):
    if "SKU_FILHO_DO_PEDIDO" in s:
        return s, "ml_service: SKU filho da variação (já estava)"
    m = re.search(r'\n([ \t]+)if var_sku:\n[ \t]+oi\["seller_sku"\] = var_sku\n([ \t]+)elif real_sku:', s)
    if not m:
        return None, "ml_service: trecho do SKU da variação diferente; ficou como estava"
    ind = m.group(1)
    novo = (f'\n{ind}# SKU_FILHO_DO_PEDIDO: o próprio pedido traz o SKU do que foi vendido (filho, se tiver variação);\n'
            f'{ind}# o SKU do anúncio (real_sku) é o da MÃE e só vale se o pedido não trouxer nenhum\n'
            f'{ind}_it = oi.get("item") or {{}}\n'
            f'{ind}sku_pedido = str(_it.get("seller_sku") or _it.get("seller_custom_field") or "").strip()\n'
            f'{ind}if var_sku:\n{ind}    oi["seller_sku"] = var_sku\n'
            f'{ind}elif sku_pedido:\n{ind}    oi["seller_sku"] = sku_pedido\n'
            f'{m.group(2)}elif real_sku:')
    return s[:m.start()] + novo + s[m.end():], "ml_service: pedido com variação usa o SKU filho"


def remendo_faturar(s):
    if "COMERCIAL_PRONTO_FATURAR" in s:
        return s, "routes.py: Comercial conferido -> Pronto para faturar (já estava)"
    i = s.find("def scanner_bipar(")
    j = s.find('if (row["origem"] or "ML") in ("COMERCIAL", "SHOPEE"):', i) if i >= 0 else -1
    if i < 0 or j < 0:
        return None, "routes.py: fechamento do Comercial diferente; Pronto para faturar não foi ligado"
    m = re.compile(r'\n([ \t]+)return jsonify\(\{"success": True, "pedido_fechado": True, "faltando_itens": 0\}\)').search(s, j)
    if not m or m.start() - j > 900:
        return None, "routes.py: return do Comercial diferente; Pronto para faturar não foi ligado"
    ind = m.group(1)
    novo = (f'\n{ind}# COMERCIAL_PRONTO_FATURAR: conferido -> Bling "Pronto para faturar" (só a situação)\n'
            f'{ind}_fat = None\n'
            f'{ind}if (row["origem"] or "") == "COMERCIAL":\n'
            f'{ind}    try:\n'
            f'{ind}        from comercial_faturar import marcar_pronto_para_faturar\n'
            f'{ind}        _fat = marcar_pronto_para_faturar(ml_id)\n'
            f'{ind}    except Exception as _e_fat:\n'
            f'{ind}        _fat = {{"ok": False, "erro": str(_e_fat)}}\n'
            f'{ind}return jsonify({{"success": True, "pedido_fechado": True, "faltando_itens": 0, "bling_faturar": _fat}})')
    return s[:m.start()] + novo + s[m.end():], "routes.py: Comercial conferido na Expedição vai para 'Pronto para faturar' no Bling"


def env_chaves(texto):
    out = set()
    for ln in texto.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", ln)
        if m:
            out.add(m.group(1))
    return out


# ───────────────────────── aplicar ─────────────────────────
def aplicar(a):
    alvo, lhv = os.path.abspath(a.alvo), os.path.abspath(a.lhv)
    reg = Registro(alvo, a.orig, a.registro)
    feitos, avisos = [], []

    # 1) módulos e ferramentas que a MedicalD não tem (cópia exata dos da LHVMed)
    novos, diferentes = [], []
    for rel in arquivos_py(lhv):
        if rel in MIOLO or rel in (MAPA, ROBO):
            continue
        src, dst = os.path.join(lhv, rel), os.path.join(alvo, rel)
        if not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            reg.novo(rel)
            shutil.copy2(src, dst)
            novos.append(rel)
        elif blob(src) != blob(dst):
            diferentes.append(rel)
    feitos.append(f"{len(novos)} arquivos novos da LHVMed")
    if diferentes:
        avisos.append("diferentes nas duas e NÃO trocados: " + ", ".join(diferentes))

    # 2) mapa_routes.py: o da MedicalD é a versão antiga (29/10) do mesmo arquivo -> o da LHVMed
    src, dst = os.path.join(lhv, MAPA), os.path.join(alvo, MAPA)
    hm, hl = blob(dst), blob(src)
    if hm != hl:
        if hm not in MAPA_MEDICALD_CONHECIDAS and os.environ.get("FORCAR") != "1":
            raise SystemExit(f"mapa_routes.py da MedicalD ({hm}) não é a versão conhecida: parei, nada mudou (FORCAR=1 para trocar)")
        reg.mudar(MAPA)
        shutil.copy2(src, dst)
        feitos.append("mapa_routes.py da LHVMed (com Saída, Separação só hoje, Full, Tarefas, Mapa montável)")

    # 3) robô da entrada: a versão nova (login digitado como uma pessoa)
    dst = os.path.join(alvo, ROBO)
    if os.path.exists(dst):
        if blob(dst) != blob(a.robo):
            reg.mudar(ROBO)
            shutil.copy2(a.robo, dst)
    else:
        reg.novo(ROBO)
        shutil.copy2(a.robo, dst)
    feitos.append("robô da entrada (versão do login digitado)")

    # 4) app.py: blueprints + sessão
    p = os.path.join(alvo, "app.py")
    s = ler(p)
    s2, av = remendo_app(s, ler(os.path.join(lhv, "app.py")))
    if s2 != s:
        reg.mudar("app.py")
        gravar(p, s2)
    feitos += av

    # 5) correções do miolo que a LHVMed recebeu
    remendos = [("services/bling_service.py", remendo_deposito), ("services/bling_service.py", remendo_comercial),
                ("services/ml_service.py", remendo_variacao)]
    if a.faturar:
        remendos.append(("routes.py", remendo_faturar))
    for rel, fn in remendos:
        p = os.path.join(alvo, rel)
        if not os.path.exists(p):
            avisos.append(f"{rel} não existe na MedicalD")
            continue
        s = ler(p)
        s2, msg = fn(s.replace("\r\n", "\n")) if "\r\n" in s else fn(s)
        if s2 is None:
            avisos.append(msg)
            continue
        if "\r\n" in s:
            s2 = s2.replace("\n", "\r\n")
        if s2 != s:
            reg.mudar(rel)
            gravar(p, s2)
        feitos.append(msg)

    # 6) .env: valores da MedicalD (só as chaves que ainda não existem)
    p = os.path.join(alvo, ".env")
    s = ler(p) if os.path.exists(p) else ""
    tem = env_chaves(s)
    faltam = [(k, v, c) for k, v, c in ENV_MEDICALD if k not in tem]
    if faltam:
        reg.mudar(".env")
        bloco = ("" if (not s or s.endswith("\n")) else "\n") + "\n# >>> ligar_medicald.sh: valores da MedicalD para os módulos da LHVMed\n"
        for k, v, c in faltam:
            if c:
                bloco += f"# {c}\n"
            bloco += f'{k}="{v}"\n'
        bloco += "# <<< ligar_medicald.sh\n"
        gravar(p, s + bloco)
        feitos.append(".env: " + ", ".join(k for k, _, _ in faltam))
    ja = [k for k, _, _ in ENV_MEDICALD if k in tem]
    if ja:
        avisos.append(".env já tinha (mantive o valor dela): " + ", ".join(ja))

    # 7) compila tudo que entrou ou mudou
    import py_compile
    for rel in novos + [MAPA, ROBO, "app.py", "services/bling_service.py", "services/ml_service.py", "routes.py"]:
        p = os.path.join(alvo, rel)
        if os.path.exists(p):
            try:
                py_compile.compile(p, doraise=True)
            except py_compile.PyCompileError as e:
                raise SystemExit(f"erro de sintaxe em {rel}: {e.msg[:300]}")
    for x in feitos:
        print("  ok  " + x)
    for x in avisos:
        print("  !!  " + x)


# ───────────────────────── testar ─────────────────────────
def _norm(rule):
    r = re.sub(r"<[^>]+>", ":p", rule).rstrip("/") or "/"
    return r


def _bate(ep, regras):
    b = ep.split("/")
    for k in regras:
        a = k.split("/")
        if len(a) == len(b) and all(x == y or x == ":p" or y == ":p" for x, y in zip(a, b)):
            return True
    return False


def _rotas_estaticas(raiz):
    """Caminhos de rota escritos nos .py da LHVMed (para saber se uma rota que falta existe lá)."""
    out = set()
    for rel in arquivos_py(raiz):
        try:
            t = ler(os.path.join(raiz, rel))
        except Exception:
            continue
        for m in re.finditer(r"""\.(?:route|get|post|put|delete|patch)\(\s*[rf]?["'](/[^"']*)""", t):
            p = _norm(m.group(1))
            out.add(p)
            out.add(p if p.startswith("/api") else "/api" + p)
    return out


def _mascara(t):
    t = re.sub(r"(postgres(?:ql)?(?:\+\w+)?://)[^\s'\"]+", r"\1***", str(t))
    return re.sub(r"(?i)((?:senha|password|secret|token|key)\w*\s*[=:]\s*)[^\s,'\"]+", r"\1***", t)


def testar(a):
    alvo, lhv = os.path.abspath(a.alvo), os.path.abspath(a.lhv)
    os.chdir(alvo)
    sys.path.insert(0, alvo)
    try:
        from dotenv import dotenv_values
        for k, v in dotenv_values(os.path.join(alvo, ".env")).items():
            if v is not None and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        print("  !!  não li o .env:", type(e).__name__)
    graves, avisos = [], []

    for mod in ("estoque_sync", "virada_dia", "comercial_faturar"):
        try:
            importlib.import_module(mod)
        except Exception as e:
            graves.append(f"{mod}.py não importa: {type(e).__name__}: {_mascara(e)[:200]}")

    try:
        from app import create_app
        app = create_app()
    except Exception as e:
        import traceback
        print("  XX  o app da MedicalD NÃO sobe com os módulos:")
        print(_mascara("".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]))
        return 1
    regras = {_norm(r.rule) for r in app.url_map.iter_rules()}
    print(f"  ok  o app sobe ({len(regras)} rotas)")

    faltam = [ep for ep in PRECISA if not _bate(ep, regras)]
    if faltam:
        nl = _rotas_estaticas(lhv)
        g = [ep for ep in faltam if _bate(ep, nl)]
        o = [ep for ep in faltam if ep not in g]
        if g:
            graves.append("rotas que a LHVMed tem e a MedicalD continua sem: " + ", ".join(g))
        if o:
            avisos.append("rotas que a tela chama e nenhuma das duas tem (não muda nada): " + ", ".join(o))
    else:
        print(f"  ok  as {len(PRECISA)} rotas que as telas novas usam existem")

    try:
        import services.bling_service as bsm
        from services.ml_service import MercadoLivreService
        fb = [m for m in METODOS_BLING if not hasattr(bsm.BlingService, m)] + [f for f in FUNCOES_BLING if not hasattr(bsm, f)]
        fm = [m for m in METODOS_ML if not hasattr(MercadoLivreService, m)]
        if fb:
            graves.append("bling_service da MedicalD sem: " + ", ".join(fb))
        if fm:
            graves.append("ml_service da MedicalD sem: " + ", ".join(fm))
        if not fb and not fm:
            print("  ok  bling_service e ml_service da MedicalD têm tudo o que os módulos chamam")
    except Exception as e:
        graves.append(f"não consegui olhar o bling_service/ml_service: {type(e).__name__}: {_mascara(e)[:200]}")

    # leituras como admin
    uid = None
    try:
        dsn = re.sub(r"^postgres(?:ql)?(?:\+\w+)?://", "postgresql://", os.environ.get("DATABASE_URL", ""))
        try:
            import psycopg2 as pg
        except ImportError:
            import psycopg as pg
        con = pg.connect(dsn)
        cur = con.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE lower(role::text)='admin' ORDER BY id LIMIT 1")
        except Exception:
            con.rollback()
            cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
        r = cur.fetchone()
        uid = r[0] if r else None
        con.close()
    except Exception as e:
        avisos.append(f"não achei um admin para as leituras de teste: {type(e).__name__}")
    if uid is not None:
        c = app.test_client()
        with c.session_transaction() as ss:
            ss["_user_id"] = str(uid)
            ss["_fresh"] = True
        linhas = []
        for url in LEITURAS:
            try:
                resp = c.get(url)
                st = resp.status_code
            except Exception as e:
                st, resp = f"erro {type(e).__name__}", None
            linhas.append(f"{url} {st}")
            if isinstance(st, str) or st >= 500:
                corpo = ""
                try:
                    corpo = resp.get_data(as_text=True)[:200] if resp is not None else ""
                except Exception:
                    pass
                graves.append(f"GET {url} -> {st} {_mascara(corpo)}")
            elif st == 404 and _bate(url, regras):
                avisos.append(f"GET {url} -> 404 (a rota existe; pode ser só 'não encontrado' de dado)")
            elif st in (401, 403):
                avisos.append(f"GET {url} -> {st} (o login de teste não valeu aqui)")
        print("  ..  leituras de teste: " + " | ".join(linhas))

    # o que mais o app.py da LHVMed tem (só para conferência; não muda nada)
    try:
        d = list(difflib.unified_diff(ler(os.path.join(alvo, "app.py")).splitlines(), ler(os.path.join(lhv, "app.py")).splitlines(),
                                      "app.py MedicalD (já com os módulos)", "app.py LHVMed", n=0, lineterm=""))
        if d:
            print(f"  ..  app.py: {sum(1 for x in d if x[:1] in '+-') - 2} linhas diferentes da LHVMed (amostra):")
            for x in d[2:42]:
                print("      " + _mascara(re.sub(r"([\"'])[^\"']{24,}\1", r"\1…\1", x))[:150])
    except Exception:
        pass

    for x in avisos:
        print("  !!  " + x)
    for x in graves:
        print("  XX  " + x)
    return 1 if graves else 0


def pip_faltam(a):
    def ler_freeze(p):
        out = {}
        for ln in open(p, encoding="utf-8", errors="ignore"):
            ln = ln.strip()
            if "==" in ln and not ln.startswith(("#", "-e")) and " @ " not in ln:
                n, v = ln.split("==", 1)
                k = re.sub(r"[-_.]+", "-", n).lower()
                if k not in ("pkg-resources", "pip", "setuptools", "wheel"):
                    out[k] = ln
        return out
    lhv, med = ler_freeze(a.lhv), ler_freeze(a.med)
    for k in sorted(set(lhv) - set(med)):
        print(lhv[k])
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("aplicar")
    x.add_argument("--alvo", required=True); x.add_argument("--lhv", required=True); x.add_argument("--robo", required=True)
    x.add_argument("--orig"); x.add_argument("--registro"); x.add_argument("--faturar", action="store_true")
    t = sub.add_parser("testar")
    t.add_argument("--alvo", required=True); t.add_argument("--lhv", required=True)
    p = sub.add_parser("pip")
    p.add_argument("--lhv", required=True); p.add_argument("--med", required=True)
    a = ap.parse_args()
    if a.cmd == "aplicar":
        aplicar(a)
        rc = 0
    elif a.cmd == "testar":
        rc = testar(a)
    else:
        rc = pip_faltam(a)
    sys.stdout.flush()
    os._exit(rc)        # não espera thread nenhuma que o app tenha aberto


if __name__ == "__main__":
    main()
