#!/usr/bin/env python3
"""
TESTE: etiqueta ML + DANFE simplificado numa etiqueta só (10x15, Elgin 203 dpi).
Só LÊ (ML e Bling) e gera arquivos — não imprime, não muda nada no WMS.

    venv/bin/python etiqueta_danfe_teste.py 2000019261234567
    -> /tmp/etq_<pedido>.png  (prévia, abre no PC)      /tmp/etq_<pedido>.zpl  (pra Elgin, se quiser testar)

Como monta:
  1) etiqueta do ML em ZPL (/shipment_labels) -> Labelary vira imagem -> reduzida pra 62% da altura (QR continua legível)
  2) NF-e no Bling (pelo nº do pedido ML) -> chave, nº/série, data, destinatário, valor
  3) bloco DANFE simplificado desenhado embaixo (Code128 da chave); tudo vira ^GF (gráfico) em ZPL
"""
from __future__ import annotations

import io
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

from PIL import Image, ImageDraw, ImageFont

W, H = 812, 1218            # 100 x 150 mm a 203 dpi
ZPL_ML = None
INV_ML = None
ML_H = int(H * 0.67)        # parte de cima: etiqueta do ML
EMITENTE = os.getenv("EMPRESA_NOME", "LHVMED Imp. e Com. de Equip. Médicos Ltda")
EMITENTE_CNPJ = os.getenv("EMPRESA_CNPJ", "")


def fonte(tam, bold=False):
    for c in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else []) + ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]:
        if os.path.exists(c):
            return ImageFont.truetype(c, tam)
    return ImageFont.load_default()


def ml_label_png(ml_order_id: str) -> Image.Image:
    from services.ml_service import MercadoLivreService
    ml = MercadoLivreService()
    sid = ml.get_shipping_id_from_order(ml_order_id)
    if not sid:
        raise SystemExit("pedido sem shipping id no ML")
    # bytes crus (o helper do serviço decodifica como texto e estraga o ZIP)
    resp = ml._request("GET", "/shipment_labels", params={"shipment_ids": str(sid), "response_type": "zpl2"})
    raw = resp.content
    if raw[:1] == b"{":
        raise SystemExit(f"ML não devolveu etiqueta (HTTP {resp.status_code}): {raw[:400].decode('utf-8','ignore')}")
    zpl = raw
    if raw[:2] == b"PK":                      # o ML manda um ZIP com "Etiqueta de envio.txt" dentro
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            nome = next((n for n in z.namelist() if n.lower().endswith((".txt", ".zpl"))), z.namelist()[0])
            zpl = z.read(nome).decode("utf-8", "ignore")
    elif isinstance(zpl, bytes):
        zpl = zpl.decode("utf-8", "ignore")
    open(f"/tmp/etq_{ml_order_id}_ml.zpl", "w").write(zpl)
    print(f"   zpl do ML: {len(zpl)} chars, começa com {zpl[:40]!r}", flush=True)
    global ZPL_ML
    ZPL_ML = zpl
    melhor = None
    for dpmm in ("8dpmm", "12dpmm"):
        r = requests.post(f"http://api.labelary.com/v1/printers/{dpmm}/labels/4x6/0/", data=zpl.encode("utf-8"),
                          headers={"Accept": "image/png"}, timeout=30)
        if r.status_code != 200:
            print(f"   labelary {dpmm}: HTTP {r.status_code} {r.text[:120]}", flush=True)
            continue
        im = Image.open(io.BytesIO(r.content)).convert("L")
        tinta = sum(1 for px in im.getdata() if px < 128)
        print(f"   labelary {dpmm}: {im.size}, tinta {tinta}", flush=True)
        if melhor is None or tinta > melhor[0]:
            melhor = (tinta, im)
    if not melhor:
        raise SystemExit("Labelary não renderizou o ZPL do ML")
    im = melhor[1]
    return recortar_conteudo(im)


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


def nfe_do_pedido(ml_order_id: str) -> dict:
    """NF-e do pedido. Fonte 1: o que o ML já tem do pedido (número/série e, se houver, o XML).
    Fonte 2: Bling — mas só aceita se o número bater com o do ML (o resolvedor por 'score' já pegou NF errada)."""
    import xml.etree.ElementTree as ET
    from services.ml_service import MercadoLivreService
    ml = MercadoLivreService()
    info = {}
    try:
        r = ml._request("GET", f"/users/{ml.user_id}/invoices/orders/{ml_order_id}")
        if r.status_code == 200:
            j = r.json()
            global INV_ML
            INV_ML = j
            print(f"   ML invoice: { {k: j[k] for k in j if k not in ('invoice_xml','xml')} }", flush=True)
            at = j.get("attributes") or {}
            rec = j.get("recipient") or {}
            iss = j.get("issuer") or {}
            rid = rec.get("identifications") or {}
            info = {"numero": str(j.get("invoice_number") or ""), "serie": str(j.get("invoice_series") or ""),
                    "chaveAcesso": at.get("invoice_key") or j.get("invoice_key") or "",
                    "dataEmissao": str(j.get("issued_date") or at.get("authorization_date") or "")[:10],
                    "valorNota": j.get("amount"),
                    "contato": {"nome": str(rec.get("name") or "").split(" (")[0], "numeroDocumento": rid.get("cpf") or rid.get("cnpj") or ""},
                    "emitente": {"nome": iss.get("name") or EMITENTE, "cnpj": ((iss.get("identifications") or {}).get("cnpj") or EMITENTE_CNPJ)},
                    "fonte": "ml"}
            if info["chaveAcesso"] and info["numero"] and str(j.get("status")) == "authorized":
                return info                       # o ML já tem tudo (é a NF que ele mesmo imprime na etiqueta)
            xml = j.get("invoice_xml") or j.get("xml")
            if xml:
                try:
                    ns = {"n": "http://www.portalfiscal.inf.br/nfe"}
                    root = ET.fromstring(xml if isinstance(xml, str) else xml.decode("utf-8", "ignore"))
                    inf = root.find(".//n:infNFe", ns)
                    g = lambda path: (inf.findtext(path, default="", namespaces=ns) if inf is not None else "")
                    info.update({
                        "chaveAcesso": (inf.get("Id") or "")[3:] if inf is not None else info.get("chaveAcesso"),
                        "numero": g("n:ide/n:nNF") or info["numero"], "serie": g("n:ide/n:serie") or info["serie"],
                        "dataEmissao": g("n:ide/n:dhEmi")[:10], "valorNota": g("n:total/n:ICMSTot/n:vNF"),
                        "contato": {"nome": g("n:dest/n:xNome"), "numeroDocumento": g("n:dest/n:CPF") or g("n:dest/n:CNPJ")},
                        "emitente": {"nome": g("n:emit/n:xNome"), "cnpj": g("n:emit/n:CNPJ")},
                        "fonte": "ml_xml",
                    })
                    return info
                except Exception as e:
                    print(f"   xml do ML não parseou: {e}", flush=True)
        else:
            print(f"   ML invoice: HTTP {r.status_code} {r.text[:150]}", flush=True)
    except Exception as e:
        print(f"   ML invoice: erro {e}", flush=True)

    from services.bling_service import BlingService
    b = BlingService()
    pv_id, nfe_id = b.resolver_pv_e_nfe_por_ml_order_id(ml_order_id)
    if not nfe_id:
        raise SystemExit("pedido ainda sem NF-e no Bling")
    d = b.obter_nfe_detalhe(nfe_id)
    d = d.get("data") or d
    if info.get("numero") and str(d.get("numero")) != info["numero"]:
        raise SystemExit(f"NF do Bling ({d.get('numero')}) NÃO bate com a NF que está no ML ({info['numero']}) — não imprimo")
    d["fonte"] = "bling"
    return d


def code128(texto: str, altura: int, largura_max: int) -> Image.Image:
    import barcode
    from barcode.writer import ImageWriter
    bc = barcode.get("code128", texto, writer=ImageWriter())
    img = bc.render({"module_height": 12.0, "module_width": 0.33, "quiet_zone": 2, "write_text": False, "dpi": 203}).convert("L")
    k = min(largura_max / img.width, altura / img.height)
    return img.resize((int(img.width * k), int(img.height * k)))


import re


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


def montar_bonito(zpl: str, inv: dict, nfe: dict, ml_order_id: str) -> Image.Image | None:
    """Layout próprio (igual ao mockup): QR + destinatário + CEP grande + rota + barcode, e DANFE embaixo."""
    z = ler_zpl(zpl)
    if not z["qr"] or not z["barcode"]:
        print(f"   zpl sem QR/barcode reconhecíveis ({z}) — usando a etiqueta do ML como imagem", flush=True)
        return None
    c = classificar(z["textos"])
    print(f"   campos do ML: {c}", flush=True)
    print(f"   CONTEÚDO DO QR (igual ao da etiqueta original): {z['qr']}", flush=True)
    print(f"   BARCODE DO ENVIO: {z['barcode']}", flush=True)
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
    global ML_H
    ML_H = y
    return canvas


def montar(ml_png: Image.Image, nfe: dict, ml_order_id: str) -> Image.Image:
    global ML_H
    canvas = None
    if ZPL_ML and INV_ML:
        canvas = montar_bonito(ZPL_ML, INV_ML, nfe, ml_order_id)
    if canvas is None:
        ML_H = int(H * 0.67)
        canvas = Image.new("L", (W, H), 255)
        k = min((W - 16) / ml_png.width, (ML_H - 12) / ml_png.height)
        ml2 = ml_png.resize((int(ml_png.width * k), int(ml_png.height * k)))
        canvas.paste(ml2, ((W - ml2.width) // 2, 0))
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


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    oid = sys.argv[1].strip()
    print(">> etiqueta do ML…", flush=True)
    ml_png = ml_label_png(oid)
    print(">> NF-e (ML primeiro, Bling só se precisar)…", flush=True)
    nfe = nfe_do_pedido(oid)
    print(f"   NF {nfe.get('numero')} série {nfe.get('serie')} chave {nfe.get('chaveAcesso')} fonte={nfe.get('fonte')}", flush=True)
    img = montar(ml_png, nfe, oid)
    img.save(f"/tmp/etq_{oid}.png")
    open(f"/tmp/etq_{oid}.zpl", "w").write(para_zpl(img))
    # A4 em tamanho real (100 x 150 mm) pra testar numa impressora comum: imprimir em escala 100%
    a4 = Image.new("L", (1654, 2339), 255)
    a4.paste(img, ((a4.width - img.width) // 2, 120))
    dd = ImageDraw.Draw(a4)
    dd.rectangle([(a4.width - img.width) // 2 - 1, 119, (a4.width + img.width) // 2, 120 + img.height], outline=160)
    dd.text((a4.width // 2, 120 + img.height + 40), "Etiqueta 100 x 150 mm em tamanho real — imprimir em escala 100%", font=fonte(26), fill=120, anchor="ma")
    a4.save(f"/tmp/etq_{oid}_A4.pdf", resolution=203)
    print(f">> prévia: /tmp/etq_{oid}.png   zpl: /tmp/etq_{oid}.zpl   A4 pra teste: /tmp/etq_{oid}_A4.pdf", flush=True)


if __name__ == "__main__":
    main()
