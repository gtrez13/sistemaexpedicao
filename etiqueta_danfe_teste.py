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
ML_H = int(H * 0.62)        # parte de cima: etiqueta do ML
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
    bbox = im.point(lambda p: 0 if p < 128 else 255).getbbox()   # recorta a parte impressa
    if bbox:
        im = im.crop((max(0, bbox[0] - 10), max(0, bbox[1] - 10), min(im.width, bbox[2] + 10), min(im.height, bbox[3] + 10)))
    return im


def nfe_do_pedido(ml_order_id: str) -> dict:
    from services.bling_service import BlingService
    b = BlingService()
    pv_id, nfe_id = b.resolver_pv_e_nfe_por_ml_order_id(ml_order_id)
    if not nfe_id:
        raise SystemExit("pedido ainda sem NF-e no Bling")
    d = b.obter_nfe_detalhe(nfe_id)
    return d.get("data") or d


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
    k = min(W / ml_png.width, ML_H / ml_png.height)
    ml2 = ml_png.resize((int(ml_png.width * k), int(ml_png.height * k)))
    canvas.paste(ml2, ((W - ml2.width) // 2, 0))
    d = ImageDraw.Draw(canvas)
    y = ML_H + 6
    for x in range(20, W - 20, 16):
        d.line([(x, y), (x + 8, y)], fill=0, width=3)
    y += 14
    f_t, f_s, f_m, f_b = fonte(30, True), fonte(22), fonte(24), fonte(28, True)
    chave = str(nfe.get("chaveAcesso") or "").replace(" ", "")
    d.text((24, y), "DANFE SIMPLIFICADO — NF-e", font=f_t, fill=0)
    d.text((24, y + 36), f"Nº {nfe.get('numero', '')}  Série {nfe.get('serie', '')}  Emissão {str(nfe.get('dataEmissao', ''))[:10]}", font=f_s, fill=0)
    y += 68
    d.text((24, y), " ".join(chave[i:i + 4] for i in range(0, len(chave), 4)), font=fonte(21), fill=0)
    y += 30
    if chave:
        bc = code128(chave, 110, W - 48)
        canvas.paste(bc, ((W - bc.width) // 2, y))
        y += bc.height + 10
    cont = nfe.get("contato") or {}
    d.text((24, y), "EMITENTE", font=fonte(16, True), fill=0)
    d.text((24, y + 18), (EMITENTE + (f" · CNPJ {EMITENTE_CNPJ}" if EMITENTE_CNPJ else ""))[:60], font=f_s, fill=0)
    y += 50
    d.text((24, y), "DESTINATÁRIO", font=fonte(16, True), fill=0)
    d.text((24, y + 18), f"{cont.get('nome', '')} · {cont.get('numeroDocumento', '')}"[:60], font=f_s, fill=0)
    y += 50
    d.line([(24, y), (W - 24, y)], fill=0, width=2)
    y += 8
    total = nfe.get("valorNota") or nfe.get("valorTotal") or ""
    d.text((24, y), f"Pedido ML {ml_order_id}", font=f_s, fill=0)
    d.text((W - 24, y), f"VALOR TOTAL R$ {total}", font=f_b, fill=0, anchor="ra")
    d.text((24, H - 34), "Consulte pela chave em www.nfe.fazenda.gov.br", font=fonte(18), fill=0)
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
    print(">> NF-e no Bling…", flush=True)
    nfe = nfe_do_pedido(oid)
    print(f"   NF {nfe.get('numero')} série {nfe.get('serie')} chave {nfe.get('chaveAcesso')}", flush=True)
    img = montar(ml_png, nfe, oid)
    img.save(f"/tmp/etq_{oid}.png")
    open(f"/tmp/etq_{oid}.zpl", "w").write(para_zpl(img))
    print(f">> prévia: /tmp/etq_{oid}.png   zpl: /tmp/etq_{oid}.zpl", flush=True)


if __name__ == "__main__":
    main()
