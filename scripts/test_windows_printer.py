import win32print
import win32ui
from PIL import Image, ImageWin

PRINTER_NAME = "4BARCODE 4B-2074B"


def test_print():
    print(f"Tentando usar impressora: {PRINTER_NAME}")

    try:
        printer = win32print.OpenPrinter(PRINTER_NAME)
        print("✅ Impressora encontrada!")
        win32print.ClosePrinter(printer)
    except Exception as e:
        print("❌ Não encontrou a impressora")
        print(e)
        return

    # cria imagem teste 4x6
    img = Image.new("RGB", (812, 1218), "white")
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    draw.text((100, 200), "TESTE DE IMPRESSAO OK", fill="black")

    hDC = win32ui.CreateDC()
    hDC.CreatePrinterDC(PRINTER_NAME)

    printable_area = hDC.GetDeviceCaps(8), hDC.GetDeviceCaps(10)
    printer_size = hDC.GetDeviceCaps(110), hDC.GetDeviceCaps(111)

    hDC.StartDoc("Teste")
    hDC.StartPage()

    dib = ImageWin.Dib(img)
    dib.draw(hDC.GetHandleOutput(), (0, 0, printer_size[0], printer_size[1]))

    hDC.EndPage()
    hDC.EndDoc()
    hDC.DeleteDC()

    print("🖨️ Se tudo deu certo, saiu uma etiqueta")


if __name__ == "__main__":
    test_print()