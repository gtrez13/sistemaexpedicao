#!/usr/bin/env bash
# Instala a ETIQUETA UNIFICADA (ML + DANFE simplificado numa 10x15). Rode DENTRO da pasta do backend (app.py).
set -e
if [ ! -f app.py ]; then echo "ERRO: rode isto dentro da pasta do backend (onde esta o app.py)"; exit 1; fi
case "$PWD" in *RussianL*) ;; *) echo "ERRO: SO LHVMed por enquanto (pasta RussianL). Pasta atual: $PWD"; exit 1;; esac
venv/bin/pip install -q pillow python-barcode qrcode 2>/dev/null || pip install -q pillow python-barcode qrcode

cat > etiqueta_unificada.py <<'ETQEOF'
"""
etiqueta_unificada.py — etiqueta do ML + DANFE simplificado numa 10x15 (203 dpi), em ZPL (^GF) pra Elgin.

    gerar(ml_order_id) -> {"zpl": str, "png": bytes, "nf": {...}, "ml": {...}}   ou levanta Falha(motivo)

Fontes (só leitura):
  * ML /shipment_labels (zpl2): QR, barcode do envio e textos (rota, data, NF, letra)
  * ML /users/{id}/invoices/orders/{order}: a NF que o próprio ML tem (número, série, chave, destinatário, valor)
Nunca usa o Bling: evita a NF errada do resolvedor por score.
"""
from __future__ import annotations

import io
import os
import re
import time
import zipfile

from PIL import Image, ImageDraw, ImageFont

W, H = 812, 1218
EMITENTE = os.environ.get("EMPRESA_NOME", "LHVMED Imp. e Com. de Equip. Médicos Ltda")
EMITENTE_CNPJ = os.environ.get("EMPRESA_CNPJ", "")


class Falha(Exception):
    pass

def fonte(tam, bold=False):
    for c in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else []) + ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]:
        if os.path.exists(c):
            return ImageFont.truetype(c, tam)
    return ImageFont.load_default()

def ler_zpl(zpl: str) -> dict:
    """Tira do ZPL do ML o que importa: dado do QR, número do código de barras e todos os textos (na ordem)."""
    out = {"qr": None, "barcode": None, "textos": []}
    campos = re.findall(r"\^FO[^^]*((?:\^[A-Z0-9][^^]*)*?)\^FD(.*?)\^FS", zpl, flags=re.S)
    for cmds, dado in campos:
        dado = dado.strip()
        if "_" in dado:   # ^FH: _C3_A9 = bytes UTF-8 em hexa
            try:
                dado = re.sub(r"(?:_[0-9A-Fa-f]{2})+", lambda m: bytes.fromhex(m.group(0).replace("_", "")).decode("utf-8", "ignore"), dado)
            except Exception:
                pass
        if "^BQ" in cmds:
            out["qr"] = re.sub(r"^[A-Z]{1,2},", "", dado)          # tira o "QA," / "MA," do ^FD
        elif re.search(r"\^B[CEU8A3]", cmds):
            if not out["barcode"]:
                out["barcode"] = re.sub(r"^>[;:]", "", dado)
        elif dado and not dado.startswith("^"):
            out["textos"].append(dado)
    return out

def classificar(textos: list[str]) -> dict:
    c = {"despachar": None, "data": None, "nf": None, "rota": [], "cadeia": None, "hora": None, "letra": None, "outros": []}
    for t in textos:
        tt = t.strip()
        if tt.lower().startswith("despachar"):
            c["despachar"] = tt
        elif re.fullmatch(r"(SEG|TER|QUA|QUI|SEX|SAB|SÁB|DOM)\s+\d\d/\d\d/\d{4}", tt, flags=re.I):
            c["data"] = tt
        elif tt.upper().startswith("NF:") or tt.upper().startswith("NF "):
            c["nf"] = tt
        elif ">" in tt and len(tt) < 40:
            c["cadeia"] = tt
        elif c["cadeia"] and c["cadeia"].rstrip().endswith(">") and re.fullmatch(r"\d{1,3}", tt):
            c["cadeia"] = c["cadeia"].rstrip() + " " + tt      # o "26" vem num campo separado
        elif re.fullmatch(r"\d\d:\d\d", tt):
            c["hora"] = tt
        elif re.fullmatch(r"[A-Z]{2,4}\d{0,2}", tt) and len(c["rota"]) < 2:
            c["rota"].append(tt)
        elif re.fullmatch(r"[A-Z]", tt):
            c["letra"] = tt
        else:
            c["outros"].append(tt)
    return c

def qr_img(dado: str, tam: int) -> Image.Image:
    import qrcode
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=1)
    q.add_data(dado); q.make(fit=True)
    im = q.make_image(fill_color="black", back_color="white").convert("L")
    return im.resize((tam, tam), Image.NEAREST)

def code128(texto: str, altura: int, largura_max: int) -> Image.Image:
    import barcode
    from barcode.writer import ImageWriter
    bc = barcode.get("code128", texto, writer=ImageWriter())
    img = bc.render({"module_height": 12.0, "module_width": 0.33, "quiet_zone": 2, "write_text": False, "dpi": 203}).convert("L")
    k = min(largura_max / img.width, altura / img.height)
    return img.resize((int(img.width * k), int(img.height * k)))

def recortar_conteudo(im: Image.Image, minimo: int = 6) -> Image.Image:
    """Recorta só onde tem impressão de verdade (ignora pixels soltos), pra etiqueta do ML encher a metade de cima."""
    bw = im.point(lambda p: 255 if p < 128 else 0, "L")       # tinta = 255
    px = bw.load()
    w, h = bw.size
    linhas = [sum(1 for x in range(w) if px[x, y]) for y in range(h)]
    cols = [sum(1 for y in range(h) if px[x, y]) for x in range(w)]
    ys = [y for y, n in enumerate(linhas) if n >= minimo]
    xs = [x for x, n in enumerate(cols) if n >= minimo]
    if not ys or not xs:
        return im
    m = 14
    return im.crop((max(0, xs[0] - m), max(0, ys[0] - m), min(w, xs[-1] + m), min(h, ys[-1] + m)))

def montar_bonito(zpl: str, inv: dict, nfe: dict, ml_order_id: str):
    """Layout próprio (igual ao mockup): QR + destinatário + CEP grande + rota + barcode, e DANFE embaixo."""
    z = ler_zpl(zpl)
    if not z["qr"] or not z["barcode"]:
        raise Falha("ZPL do ML sem QR/código de barras reconhecíveis")
    c = classificar(z["textos"])
    rec = inv.get("recipient") or {}
    ad = rec.get("address") or {}
    ship = inv.get("shipment") or {}
    tipo = {"self_service": "FLEX", "xd_drop_off": "AGÊNCIA", "drop_off": "CORREIOS", "cross_docking": "COLETA", "fulfillment": "FULL"}.get(ship.get("logistic_type"), (ship.get("logistic_type") or "").upper())

    canvas = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(canvas)
    M = 28
    y = 26
    # cabeçalho
    d.text((M, y), "mercado", font=fonte(24, True), fill=0); d.text((M, y + 26), "envios", font=fonte(24, True), fill=0)
    if tipo:
        tw = d.textlength(tipo, font=fonte(26, True))
        bx = (W - tw) // 2 - 16
        d.rounded_rectangle([bx, y, bx + tw + 32, y + 50], radius=8, outline=0, width=3)
        d.text((W // 2, y + 25), tipo, font=fonte(26, True), fill=0, anchor="mm")
    if c["despachar"]:
        txt = c["despachar"].replace("Despachar: ", "Despachar\n")
        d.multiline_text((W - M, y), txt, font=fonte(18, True), fill=0, anchor="ra", align="right")
    y += 76
    # QR + destinatário
    QR = 300
    canvas.paste(qr_img(z["qr"], QR), (M, y))
    x2 = M + QR + 22
    nome = str(rec.get("name") or "").split(" (")[0]
    f_n = fonte(27, True)
    linhas = []
    palavras, atual = nome.split(), ""
    for p_ in palavras:
        if d.textlength((atual + " " + p_).strip(), font=f_n) > W - x2 - M: linhas.append(atual); atual = p_
        else: atual = (atual + " " + p_).strip()
    linhas.append(atual)
    yy = y
    for l in linhas[:2]:
        d.text((x2, yy), l, font=f_n, fill=0); yy += 32
    yy += 4
    f_e = fonte(20)
    end = [f"{ad.get('street_name','')}, {ad.get('street_number','')}".strip(", "), (ad.get("complement") or "").strip(), f"{ad.get('neighborhood','')} · {ad.get('city','')} · {ad.get('state','')}"]
    for l in [e_ for e_ in end if e_]:
        d.text((x2, yy), l[:44], font=f_e, fill=0); yy += 25
    cep = str(ad.get("zip_code") or "")
    cep = f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep
    yy += 6
    d.text((x2, yy), f"CEP {cep}", font=fonte(40, True), fill=0); yy += 50
    d.text((x2, yy), "Remetente: LHVMED · Osasco/SP", font=fonte(18), fill=0)
    y += QR + 16
    # rota (o que o ML usa pra separar): SPR2 grande, cadeia, data/NF, letra
    d.line([(M, y), (W - M, y)], fill=0, width=2); y += 10
    rota_grande = c["rota"][-1] if c["rota"] else ""
    if rota_grande:
        d.text((M, y - 4), rota_grande, font=fonte(84, True), fill=0)
        if c["hora"]: d.text((M + d.textlength(rota_grande, font=fonte(84, True)) + 18, y + 36), c["hora"], font=fonte(26, True), fill=0)
    if c["letra"]:
        d.rectangle([W - M - 70, y + 8, W - M, y + 68], fill=0)
        d.text((W - M - 35, y + 38), c["letra"], font=fonte(36, True), fill=255, anchor="mm")
    y += 92
    if c["cadeia"]: d.text((M, y), c["cadeia"], font=fonte(22, True), fill=0); y += 28
    info = "   ".join(x for x in [c["data"], c["nf"]] if x)
    if info: d.text((M, y), info, font=fonte(22, True), fill=0); y += 30
    rem = [o for o in c["outros"] if "#" in o][:1]       # linha do vendedor no ML
    for o in rem:
        d.text((M, y), o[:70], font=fonte(16), fill=0); y += 20
    # barcode do envio
    y += 6
    bc = code128(z["barcode"], 100, W - 2 * M)
    canvas.paste(bc, ((W - bc.width) // 2, y)); y += bc.height + 2
    d.text((W // 2, y), z["barcode"], font=fonte(24, True), fill=0, anchor="ma"); y += 30
    d.text((M, y), f"Venda {ml_order_id}", font=fonte(18), fill=0)
    d.text((W - M, y), f"Envio {ship.get('id','')}", font=fonte(18), fill=0, anchor="ra")
    y += 30
    return canvas, y

def montar(zpl: str, inv: dict, nfe: dict, ml_order_id: str) -> Image.Image:
    canvas, ML_H = montar_bonito(zpl, inv, nfe, ml_order_id)
    d = ImageDraw.Draw(canvas)
    y = ML_H + 4
    for x in range(20, W - 20, 16):
        d.line([(x, y), (x + 8, y)], fill=0, width=3)
    y += 12
    chave = str(nfe.get("chaveAcesso") or "").replace(" ", "")
    cont = nfe.get("contato") or {}
    emit = nfe.get("emitente") or {}
    def fmt_doc(v):
        v = "".join(ch for ch in str(v or "") if ch.isdigit())
        if len(v) == 11: return f"{v[:3]}.{v[3:6]}.{v[6:9]}-{v[9:]}"
        if len(v) == 14: return f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}"
        return str(v or "")
    total = nfe.get("valorNota") or nfe.get("valorTotal") or ""
    try:
        total = f"{float(str(total).replace(',', '.')):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except ValueError:
        pass
    dt = str(nfe.get("dataEmissao", ""))[:10]
    if len(dt) == 10 and dt[4] == "-":
        dt = f"{dt[8:10]}/{dt[5:7]}/{dt[0:4]}"
    # cabeçalho: título à esquerda, nº/série/data à direita
    d.text((24, y), "DANFE SIMPLIFICADO", font=fonte(26, True), fill=0)
    d.text((W - 24, y + 4), f"NF-e {nfe.get('numero', '')}  ·  Série {nfe.get('serie', '')}  ·  {dt}", font=fonte(20, True), fill=0, anchor="ra")
    y += 36
    if chave:
        d.text((24, y), "CHAVE DE ACESSO", font=fonte(13, True), fill=0); y += 15
        bc = code128(chave, 120, W - 40)
        canvas.paste(bc, ((W - bc.width) // 2, y))
        y += bc.height + 4
        d.text((W // 2, y), " ".join(chave[i:i + 4] for i in range(0, len(chave), 4)), font=fonte(20), fill=0, anchor="ma")
        y += 30
    # duas colunas: emitente | destinatário
    col = (W - 48) // 2
    d.text((24, y), "EMITENTE", font=fonte(14, True), fill=0)
    d.text((24 + col, y), "DESTINATÁRIO", font=fonte(14, True), fill=0)
    y += 17
    f_c = fonte(18)
    def cabe(txt, maxw):
        txt = str(txt or "")
        if d.textlength(txt, font=f_c) <= maxw:
            return txt
        while txt and d.textlength(txt + "…", font=f_c) > maxw:
            txt = txt[:-1]
        return txt + "…"
    en = EMITENTE   # nome curto (o do ML vem gigante); CNPJ vem do ML
    ec = emit.get("cnpj") or EMITENTE_CNPJ
    d.text((24, y), cabe(en, col - 12), font=f_c, fill=0); d.text((24, y + 22), f"CNPJ {fmt_doc(ec)}" if ec else "", font=f_c, fill=0)
    d.text((24 + col, y), cabe(cont.get("nome", ""), col - 12), font=f_c, fill=0); d.text((24 + col, y + 22), fmt_doc(cont.get("numeroDocumento", "")), font=f_c, fill=0)
    y += 52
    d.line([(24, y), (W - 24, y)], fill=0, width=2)
    y += 8
    d.text((24, y + 4), f"Pedido ML {ml_order_id}", font=fonte(19), fill=0)
    d.text((W - 24, y), f"TOTAL R$ {total}", font=fonte(30, True), fill=0, anchor="ra")
    d.text((24, y + 44), "Consulte pela chave de acesso em www.nfe.fazenda.gov.br", font=fonte(16), fill=0)
    return canvas

def para_zpl(img: Image.Image) -> str:
    bw = img.point(lambda p: 0 if p < 128 else 255, "1")
    w, h = bw.size
    bpr = (w + 7) // 8
    dados = bw.tobytes()
    # PIL '1' = 1 bit branco; ZPL ^GF espera 1 = preto
    hexa = bytes(b ^ 0xFF for b in dados).hex().upper()
    return f"^XA^PW{w}^LL{h}^FO0,0^GFA,{len(dados)},{len(dados)},{bpr},{hexa}^FS^XZ"


def _ml():
    from services.ml_service import MercadoLivreService
    return MercadoLivreService()


def baixar_zpl_ml(ml, ml_order_id: str) -> str:
    sid = ml.get_shipping_id_from_order(ml_order_id)
    if not sid:
        raise Falha("pedido sem envio no ML")
    resp = ml._request("GET", "/shipment_labels", params={"shipment_ids": str(sid), "response_type": "zpl2"})
    raw = resp.content
    if raw[:1] == b"{":
        try:
            msg = resp.json().get("message") or resp.text[:200]
        except Exception:
            msg = resp.text[:200]
        raise Falha(f"ML não devolveu a etiqueta: {msg}")
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            nome = next((n for n in z.namelist() if n.lower().endswith((".txt", ".zpl"))), z.namelist()[0])
            return z.read(nome).decode("utf-8", "ignore")
    return raw.decode("utf-8", "ignore")


def nf_do_ml(ml, ml_order_id: str, tentativas: int = 3, espera: float = 2.0) -> tuple[dict, dict]:
    """NF que o ML tem do pedido. O Bling manda pro ML logo após autorizar; por isso tenta algumas vezes."""
    ultimo = "sem resposta"
    for i in range(tentativas):
        r = ml._request("GET", f"/users/{ml.user_id}/invoices/orders/{ml_order_id}")
        if r.status_code == 200:
            j = r.json()
            at = j.get("attributes") or {}
            rec = j.get("recipient") or {}
            iss = j.get("issuer") or {}
            rid = rec.get("identifications") or {}
            nfe = {"numero": str(j.get("invoice_number") or ""), "serie": str(j.get("invoice_series") or ""),
                   "chaveAcesso": at.get("invoice_key") or "",
                   "dataEmissao": str(j.get("issued_date") or at.get("authorization_date") or "")[:10],
                   "valorNota": j.get("amount"),
                   "contato": {"nome": str(rec.get("name") or "").split(" (")[0], "numeroDocumento": rid.get("cpf") or rid.get("cnpj") or ""},
                   "emitente": {"nome": iss.get("name") or EMITENTE, "cnpj": ((iss.get("identifications") or {}).get("cnpj") or EMITENTE_CNPJ)}}
            if nfe["chaveAcesso"] and nfe["numero"] and str(j.get("status")) == "authorized":
                return nfe, j
            ultimo = f"NF no ML ainda não autorizada (status={j.get('status')})"
        else:
            ultimo = f"ML invoice HTTP {r.status_code}"
        if i < tentativas - 1:
            time.sleep(espera)
    raise Falha(ultimo)


def gerar(ml_order_id: str) -> dict:
    ml = _ml()
    zpl_ml = baixar_zpl_ml(ml, ml_order_id)
    nfe, inv = nf_do_ml(ml, ml_order_id)
    img = montar(zpl_ml, inv, nfe, ml_order_id)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return {"zpl": para_zpl(img), "png": buf.getvalue(), "nf": {k: nfe[k] for k in ("numero", "serie", "chaveAcesso")},
            "ml": {"shipment_id": (inv.get("shipment") or {}).get("id")}}
ETQEOF
cat > etiqueta_routes.py <<'ETQEOF'
"""
Etiqueta unificada (ML + DANFE simplificado) — LHVMed.

    GET /api/etiqueta-unificada/<ml_id>        -> {"success": true, "zpl": "...", "nf": {...}}  (pra Elgin via QZ)
    GET /api/etiqueta-unificada/<ml_id>.png    -> a imagem (prévia / debug)

Desligar sem reinstalar nada: ETIQUETA_UNIFICADA=0 no drop-in do serviço (a tela volta a imprimir DANFE A4 + etiqueta).
Só lê ML. Qualquer falha devolve success=false e a tela cai no fluxo antigo sozinha.
"""
from __future__ import annotations

import os

from flask import Blueprint, Response, jsonify
from flask_login import login_required

etiqueta_routes = Blueprint("etiqueta_routes", __name__)


def _ligada() -> bool:
    return os.environ.get("ETIQUETA_UNIFICADA", "1").strip().lower() not in ("0", "false", "nao", "não", "off")


@etiqueta_routes.get("/etiqueta-unificada/<ml_id>")
@login_required
def etiqueta_unificada(ml_id):
    if not _ligada():
        return jsonify({"success": False, "error": "desligada (ETIQUETA_UNIFICADA=0)"}), 404
    try:
        from etiqueta_unificada import gerar, Falha
        r = gerar(str(ml_id).strip())
        return jsonify({"success": True, "zpl": r["zpl"], "nf": r["nf"], "ml": r["ml"]})
    except Exception as e:
        print(f"⚠️ etiqueta unificada {ml_id}: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 502


@etiqueta_routes.get("/etiqueta-unificada/<ml_id>.png")
@login_required
def etiqueta_unificada_png(ml_id):
    try:
        from etiqueta_unificada import gerar
        r = gerar(str(ml_id).strip())
        return Response(r["png"], mimetype="image/png")
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502
ETQEOF
python3 -m py_compile etiqueta_unificada.py etiqueta_routes.py && echo ">> etiqueta_unificada.py / etiqueta_routes.py ok"

python3 - <<'PYEOF'
f="app.py"; s=open(f, encoding="utf-8").read()
if "etiqueta_routes" in s:
    print(">> app.py ja tinha o etiqueta_routes (ok)")
else:
    linha = '    from etiqueta_routes import etiqueta_routes as etiqueta_bp\n    app.register_blueprint(etiqueta_bp, url_prefix="/api")\n'
    ancoras = ['    app.register_blueprint(picking_bp, url_prefix="/api")\n',
               '    app.register_blueprint(devolucoes_bp, url_prefix="/api")\n',
               '    app.register_blueprint(mapa_bp, url_prefix="/api")\n',
               "    app.register_blueprint(api_routes)\n"]
    for a in ancoras:
        if a in s:
            open(f + ".bak_etiqueta", "w", encoding="utf-8").write(s)
            open(f, "w", encoding="utf-8").write(s.replace(a, a + linha, 1))
            print(">> app.py patchado (backup em app.py.bak_etiqueta)")
            break
    else:
        print("!! Nao achei onde registrar. Adicione dentro de create_app():"); print(linha); raise SystemExit(1)
PYEOF
python3 -m py_compile app.py && echo ">> app.py COMPILOU"
echo ""
echo "Reinicie: sudo systemctl restart wms-russia"
echo "REVERTER (1 comando): cp app.py.bak_etiqueta app.py && sudo systemctl restart wms-russia"
echo "Desligar sem reverter: ETIQUETA_UNIFICADA=0 no drop-in do servico e reiniciar"
