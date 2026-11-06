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
    img = bc.render({"module_height": 8.0, "module_width": 0.2, "quiet_zone": 1, "write_text": False, "dpi": 203}).convert("L")
    k = min(largura_max / img.width, altura / img.height)
    return img.resize((int(img.width * k), int(img.height * k)))


def montar(ml_png: Image.Image, nfe: dict, ml_order_id: str) -> Image.Image:
    canvas = Image.new("L", (W, H), 255)
    # ML em cima, reduzido proporcionalmente
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
        bc = code128(chave, 96, W - 48)
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
    print(f">> prévia: /tmp/etq_{oid}.png   zpl: /tmp/etq_{oid}.zpl", flush=True)


if __name__ == "__main__":
    main()
