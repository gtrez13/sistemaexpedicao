"""
Emitir NF-e via Bling (tentando resolver e garantir que a NF pertence ao PV),
e baixar DANFE (validando PDF).

Fluxo:
1) ML: resolve PACK/ORDER -> ORDER (numeroLoja)
2) Bling: acha PV EM ABERTO pelo numeroLoja
3) Acha NF-e:
   3.1) tenta extrair NF-e do PV
   3.2) se não achar: procura NF-e existente e valida pelo PV (idPedidoVenda OU numeroLoja OU doc contato)
   3.3) se não achar: tenta criar NF-e por PV
        - se falhar com "XML já existe": recupera NF-e por busca
4) Debug resumo do detalhe NF-e
5) (Opcional) tenta atualizar NF-e via PUT com itens do PV + /produtos/{id} (se editável)
6) Tenta ENVIAR direto
   - se falhar com code 9 (faltam campos obrigatórios): debug fiscal e para
7) Aguarda autorização (se não estiver autorizada)
8) Baixa DANFE simplificado e valida se é PDF mesmo
"""

from __future__ import annotations

import json
from pathlib import Path

from services.bling_service import BlingService
from services.ml_service import MercadoLivreService


# =========================================================
# Config
# =========================================================
ML_ANY_ID = "2000011696730329"  # pack ou order bipado
OUT_DIR = Path(".")
TENTAR_ATUALIZAR_NFE = True     # se quiser “sem editar”, bota False

MAX_NFE_DETAIL_CHECKS = 10      # evita varrer 200 NFs (fica lento)


# =========================================================
# Helpers básicos
# =========================================================
def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _only_digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _pretty(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _is_pdf(b: bytes) -> bool:
    return isinstance(b, (bytes, bytearray)) and len(b) >= 4 and b[:4] == b"%PDF"


def _is_html(b: bytes) -> bool:
    if not isinstance(b, (bytes, bytearray)):
        return False
    h = b.lstrip()[:200].lower()
    return h.startswith(b"<!doctype html") or h.startswith(b"<html") or (b"<html" in h)


def _is_code9_missing_fields(err: Exception | str) -> bool:
    s = str(err or "")
    return ('"code":9' in s) or ("obrigat" in s.lower() and "falt" in s.lower())


def _is_xml_already_exists(err: Exception | str) -> bool:
    s = str(err or "").lower()
    return "ja existe uma nota fiscal cadastrada com este xml" in s or "já existe uma nota fiscal cadastrada com este xml" in s


# =========================================================
# BLING: wrappers de detalhe
# =========================================================
def bl_get_pv_detail(bl: BlingService, pv_id: int) -> dict:
    resp = bl.get_pedido_venda_detalhe(int(pv_id))
    return resp.get("data") if isinstance(resp, dict) and "data" in resp else (resp or {})


def bl_get_nfe_detail(bl: BlingService, nfe_id: int) -> dict:
    det = bl.obter_nfe_detalhe(int(nfe_id))
    return det or {}


# =========================================================
# SKU helpers (fallback)
# =========================================================
def pv_skus_from_detail(pv_det: dict) -> set[str]:
    skus = set()
    for it in (pv_det.get("itens") or []):
        if not isinstance(it, dict):
            continue
        sku = it.get("codigo") or it.get("sku") or (it.get("item") or {}).get("codigo")
        if sku:
            skus.add(str(sku).strip())
    return {s for s in skus if s}


def nfe_skus_from_detail(nfe_det: dict) -> set[str]:
    skus = set()
    for it in (nfe_det.get("itens") or []):
        if not isinstance(it, dict):
            continue
        prod = it.get("produto") or {}
        sku = (prod.get("codigo") if isinstance(prod, dict) else None) or it.get("codigo")
        if sku:
            skus.add(str(sku).strip())
    return {s for s in skus if s}


# =========================================================
# Validação PV ↔ NF (mais confiável)
# =========================================================
def nfe_matches_pv(nfe_det: dict, pv_id: int, pv_det: dict, numero_loja: str) -> bool:
    pv_id = int(pv_id)
    numero_loja = str(numero_loja).strip()

    # 1) vínculo forte (se existir)
    id_pv_in_nfe = (
        nfe_det.get("idPedidoVenda")
        or (nfe_det.get("pedidoVenda") or {}).get("id")
        or nfe_det.get("idVenda")
    )
    if _to_int(id_pv_in_nfe) == pv_id:
        return True

    # 2) se a NF tiver numeroLoja, bate aqui
    nfe_numero_loja = str(nfe_det.get("numeroLoja") or "").strip()
    if nfe_numero_loja and nfe_numero_loja == numero_loja:
        pv_doc = _only_digits((pv_det.get("contato") or {}).get("numeroDocumento") or "")
        nf_doc = _only_digits((nfe_det.get("contato") or {}).get("numeroDocumento") or "")
        if pv_doc and nf_doc:
            return pv_doc == nf_doc
        return True

    # 3) valida por doc do contato
    pv_doc = _only_digits((pv_det.get("contato") or {}).get("numeroDocumento") or "")
    nf_doc = _only_digits((nfe_det.get("contato") or {}).get("numeroDocumento") or "")
    if pv_doc and nf_doc and pv_doc != nf_doc:
        return False

    # 4) SKU vira fallback fraco (não bloqueia sozinho)
    pv_skus = pv_skus_from_detail(pv_det)
    nf_skus = nfe_skus_from_detail(nfe_det)
    if pv_skus and nf_skus and pv_skus.intersection(nf_skus):
        return True

    return False


# =========================================================
# Buscar NF-e candidata e validar sem matar a API
# =========================================================
def listar_candidatos_nfe(bl: BlingService, criterios: list[str]) -> list[int]:
    candidatos: list[int] = []
    for criterio in criterios:
        try:
            data = bl._get_json("/nfe", params={"criterio": criterio, "pagina": 1, "limite": 50})
            for row in (data.get("data") or []):
                nid = row.get("id") or row.get("idNotaFiscal") or row.get("nfeId")
                if nid:
                    candidatos.append(int(nid))
        except Exception:
            pass

    # remove duplicados mantendo ordem
    return list(dict.fromkeys(candidatos))


def encontrar_nfe_da_pv_validando(bl: BlingService, pv_id: int, numero_loja: str, pv_det: dict) -> int | None:
    pv_id = int(pv_id)
    numero_loja = str(numero_loja).strip()

    candidatos = listar_candidatos_nfe(bl, [numero_loja, str(pv_id)])

    # checa só os primeiros N detalhes
    for nfe_id in candidatos[:MAX_NFE_DETAIL_CHECKS]:
        try:
            det = bl_get_nfe_detail(bl, nfe_id)
        except Exception:
            continue
        if nfe_matches_pv(det, pv_id=pv_id, pv_det=pv_det, numero_loja=numero_loja):
            return int(nfe_id)

    # fallback: se existem candidatos, pega o primeiro (porque o "XML já existe" prova que existe algo)
    if candidatos:
        print("⚠️ Não consegui validar 100% a NF-e, mas existem candidatos. Usando o mais recente:", candidatos[0])
        return int(candidatos[0])

    return None


# =========================================================
# Debug resumo da NF
# =========================================================
def print_nf_resumo(bl: BlingService, nfe_id: int):
    det = bl.obter_nfe_detalhe(int(nfe_id)) or {}
    print("\n📌 NF-e DETALHE (resumo)")
    print("id =", det.get("id") or nfe_id)
    print("situacao =", det.get("situacao"), "status =", det.get("status"))
    print("numero =", det.get("numero"), "serie =", det.get("serie"))
    print("idNaturezaOperacao =", det.get("idNaturezaOperacao"), "naturezaOperacao =", det.get("naturezaOperacao"))
    print("modalidadeFrete =", (det.get("transporte") or {}).get("modalidadeFrete"))
    print("chaveAcesso =", det.get("chaveAcesso") or det.get("chave") or det.get("chave_acesso"))
    print("protocolo =", det.get("protocolo") or det.get("protocoloAutorizacao") or det.get("protocolo_autorizacao"))

    txt = json.dumps(det, ensure_ascii=False).lower()
    for k in ["erro", "erros", "mensagem", "mensagens", "reje", "valid"]:
        if k in txt:
            print("⚠️ achei indícios no JSON (procure por:", k, ")")


# =========================================================
# (Opcional) Enriquecer NF via /produtos/{id} do PV
# =========================================================
def _pick(d: dict, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _extract_item_obj(it: dict) -> dict:
    """
    No teu PV, o item vem com 'codigo' no nível de fora.
    NÃO pode retornar it['produto'] quando for só {'id': ...}, senão perde o codigo.
    """
    if not isinstance(it, dict):
        return {}

    if any(k in it for k in ("codigo", "descricao", "quantidade", "valor")):
        return it

    if isinstance(it.get("item"), dict):
        return it["item"]

    prod = it.get("produto")
    if isinstance(prod, dict) and any(k in prod for k in ("codigo", "sku", "descricao", "ncm", "classificacaoFiscal")):
        return prod

    return it


def montar_payload_nfe_completo(bl: BlingService, pv_det: dict, nfe_det: dict) -> dict:
    natureza = nfe_det.get("naturezaOperacao") or {}
    natureza_id = natureza.get("id") if isinstance(natureza, dict) else None
    if not natureza_id:
        raise Exception("NF-e sem naturezaOperacao.id (e não vou chutar).")

    contato = nfe_det.get("contato") or (pv_det.get("contato") or {})
    if not isinstance(contato, dict):
        contato = {}

    itens_pv = pv_det.get("itens") or []
    if not itens_pv:
        raise Exception("PV sem itens.")

    itens_out = []

    for it in itens_pv:
        obj = _extract_item_obj(it)

        codigo = _pick(obj, "codigo", "sku", "codigoProduto", "gtin", "ean")
        codigo = str(codigo).strip() if codigo else ""
        if not codigo:
            print("⚠️ PV item sem codigo detectado:", obj)
            continue

        qtd = _pick(obj, "quantidade", "qtde", "qtd") or 1
        val = _pick(obj, "valor", "preco", "valorUnitario", "valor_unitario") or 0

        try:
            qtd = float(str(qtd).replace(",", "."))
        except Exception:
            qtd = 1.0
        try:
            val = float(str(val).replace(",", "."))
        except Exception:
            val = 0.0

        # tenta do PV
        ncm = _pick(obj, "ncm", "classificacaoFiscal", "classificacao_fiscal")
        origem = _pick(obj, "origem")
        cst = _pick(obj, "cst")
        csosn = _pick(obj, "csosn")

        # melhor: produto.id do PV => /produtos/{id}
        prod = None
        prod_id = None
        if isinstance(it, dict):
            prod_id = _pick(it.get("produto") or {}, "id")

        if prod_id:
            try:
                prod_det = bl._get_json(f"/produtos/{int(prod_id)}")
                prod = prod_det.get("data") or prod_det
            except Exception:
                prod = None

        if isinstance(prod, dict):
            ncm = ncm or _pick(prod, "ncm", "classificacaoFiscal", "classificacao_fiscal")
            origem = origem if origem is not None else _pick(prod, "origem")
            cst = cst or _pick(prod, "cst")
            csosn = csosn or _pick(prod, "csosn")

            trib = prod.get("tributacao") or prod.get("tributos") or {}
            if isinstance(trib, dict):
                origem = origem if origem is not None else _pick(trib, "origem")
                cst = cst or _pick(trib, "cst")
                csosn = csosn or _pick(trib, "csosn")

        item_out = {"codigo": codigo, "quantidade": qtd, "valor": val}

        if ncm:
            item_out["classificacaoFiscal"] = str(ncm).strip()

        tributacao = {}
        if origem is not None:
            try:
                tributacao["origem"] = int(origem)
            except Exception:
                pass
        if cst:
            tributacao["cst"] = str(cst).strip()
        if csosn:
            tributacao["csosn"] = str(csosn).strip()
        if tributacao:
            item_out["tributacao"] = tributacao

        itens_out.append(item_out)

    if not itens_out:
        raise Exception("Não consegui montar itens da NF-e (nenhum item do PV tinha código).")

    transporte = nfe_det.get("transporte") or {}
    if not isinstance(transporte, dict):
        transporte = {}

    if transporte.get("modalidadeFrete") is None:
        transporte["modalidadeFrete"] = 1

    payload = {
        "numero": nfe_det.get("numero"),
        "serie": nfe_det.get("serie"),
        "naturezaOperacao": {"id": int(natureza_id)},
        "contato": contato,
        "itens": itens_out,
        "transporte": transporte,
    }
    return payload


# =========================================================
# MAIN
# =========================================================
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ml = MercadoLivreService()
    bl = BlingService()

    # 1) ML resolve
    print("🔁 ML: resolvendo PACK/ORDER -> ORDER...")
    order_id = ml.resolver_ml_para_numero_loja(ML_ANY_ID)
    if not order_id:
        print("❌ Não consegui resolver para ORDER.")
        return
    order_id = str(order_id).strip()
    print("✅ ORDER (numeroLoja) =", order_id)

    # 2) PV em aberto
    print("🔎 BLING: achando PV EM ABERTO pelo numeroLoja...")
    pv_id = _to_int(bl.buscar_pedido_venda_id_por_numero_loja(order_id, somente_em_aberto=True))
    print("PV_ID =", pv_id)
    if not pv_id:
        print("❌ Não achei PV em aberto pro numeroLoja.")
        return

    pv_det = bl_get_pv_detail(bl, pv_id)

    # segurança: valida numeroLoja exato
    if str(pv_det.get("numeroLoja") or "").strip() != order_id:
        print("❌ PV retornado não bate numeroLoja exato. Parando pra não emitir errado.")
        print("PV.numeroLoja =", pv_det.get("numeroLoja"))
        return

    # debug item PV
    print("\n🧪 DEBUG PV itens (primeiro item bruto):")
    try:
        print(_pretty((pv_det.get("itens") or [None])[0]))
    except Exception as e:
        print("falhou debug:", e)

    # 3) tenta NF do PV
    print("🔎 Tentando NF-e do PV...")
    nfe_id = _to_int(bl.extrair_nfe_id_do_pedido_venda(pv_id))
    print("NF-e do PV =", nfe_id)

    # 3.2) busca/valida
    if not nfe_id:
        print("🕵️ Procurando NF-e EXISTENTE (validando pelo PV)...")
        nfe_id = encontrar_nfe_da_pv_validando(bl, pv_id, order_id, pv_det=pv_det)
        print("NF-e validada =", nfe_id)

    # 3.3) cria (se necessário), mas trata XML já existe
    if not nfe_id:
        print("🧾 Não achei NF-e validada. Tentando criar rascunho por PV...")
        try:
            nfe_id = _to_int(bl.criar_nfe_por_pv(pv_id))
            print("NF-e criada =", nfe_id)
        except Exception as e:
            if _is_xml_already_exists(e):
                print("⚠️ Bling disse que o XML já existe. Então a NF-e existe — vou buscar de novo e pegar o melhor candidato.")
                nfe_id = encontrar_nfe_da_pv_validando(bl, pv_id, order_id, pv_det=pv_det)
                print("NF-e recuperada =", nfe_id)
            else:
                raise

    if not nfe_id:
        print("❌ Sem nfe_id.")
        return

    # 4) resumo NF
    print_nf_resumo(bl, nfe_id)

    # 5) (opcional) PUT pra preencher NCM/origem/cst/csosn usando /produtos/{id}
    if TENTAR_ATUALIZAR_NFE:
        try:
            nfe_det = bl_get_nfe_detail(bl, nfe_id)
            payload = montar_payload_nfe_completo(bl, pv_det=pv_det, nfe_det=nfe_det)

            print("\n🧩 Payload (resumo):")
            print(_pretty({
                "numero": payload.get("numero"),
                "serie": payload.get("serie"),
                "naturezaOperacao": payload.get("naturezaOperacao"),
                "transporte": payload.get("transporte"),
                "itens_count": len(payload.get("itens") or []),
                "primeiro_item": (payload.get("itens") or [None])[0],
            }))

            print("♻️ Tentando atualizar NF-e via PUT (se estiver editável)...")
            bl.atualizar_nfe(nfe_id, payload)
            print("✅ PUT OK")
        except Exception as e:
            print("⚠️ Não consegui atualizar NF-e (talvez travada):", e)

    # 6) enviar
    try:
        print("📤 Enviando NF-e para SEFAZ (direto)...")
        bl.enviar_nfe(nfe_id, enviar_email=False)
        print("✅ enviar_nfe OK (request aceito).")
    except Exception as e:
        print("⚠️ enviar_nfe deu erro:", e)

        if _is_code9_missing_fields(e):
            print("❌ Code 9: faltam dados obrigatórios. Debug fiscal e parando aqui.")
            try:
                bl.debug_campos_fiscais_nfe(nfe_id)
            except Exception as e2:
                print("⚠️ Falhou debug_campos_fiscais_nfe:", e2)

            print("\n➡️ Se no debug aparecer ncm/origem/cst/csosn None, o próximo passo é imprimir /produtos/{id} do PV e ver onde o Bling guarda esses campos.")
            return

    # 7) aguardar autorização
    try:
        if bl.nfe_esta_autorizada(nfe_id):
            print("✅ NF-e já está AUTORIZADA.")
        else:
            print("⏳ Aguardando autorização...")
            bl.aguardar_nfe_autorizada(nfe_id, timeout_s=180, poll_s=2.0)
            print("✅ AUTORIZADA!")
    except Exception as e:
        if bl.nfe_esta_autorizada(nfe_id):
            print("✅ Já estava AUTORIZADA (apesar do erro no poll).")
        else:
            print("❌ Não autorizou / timeout:", e)
            return

    # 8) baixar DANFE
    print("⬇️ Baixando DANFE simplificado...")
    danfe_bytes = bl.baixar_danfe_simplificado_por_nfe(nfe_id)

    if _is_html(danfe_bytes) or (not _is_pdf(danfe_bytes)):
        print("❌ O download NÃO veio como PDF. Provavelmente veio HTML/erro.")
        print("📄 Trecho do retorno (pra debug):")
        try:
            print(danfe_bytes[:400].decode("utf-8", errors="ignore"))
        except Exception:
            print(danfe_bytes[:400])
        return

    out = OUT_DIR / f"danfe_{order_id}_{nfe_id}.pdf"
    out.write_bytes(danfe_bytes)
    print("✅ DANFE salvo em:", str(out))


if __name__ == "__main__":
    main()