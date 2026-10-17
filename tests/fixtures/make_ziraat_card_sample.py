"""Write a made-up Ziraat Bankası card transaction list ("Kart Hareketleri") as a PDF.

The file is built like the bank's download: internet banking prints a web
page, so the whole list is one tall drawing (a form XObject) and every PDF
page shows a slice of it, clipped. Text pieces sit where the bank puts them:
the date at the left, the description in the middle, the amount and "TL" as
two pieces at the right; installment notes, a description split into two
pieces, one wrapped onto a second line, soft hyphens, and the page's
"TAKSİTLENDİR" and "ERTELE" buttons are all there. Every name, number and
transaction is invented.

The font is the standard Helvetica with the Turkish letters that WinAnsi
lacks placed through an encoding /Differences array, so text extraction maps
them back to Unicode.

Run ``python tests/fixtures/make_ziraat_card_sample.py`` to rewrite the file.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent
SOFT = "\xad"

# date, description lines (each a list of pieces), amount as printed, installment note, buttons
CARD_LINES = [
    ("28.07.2026", [["ÖRNEK MARKET A.Ş. KONYA"]], "437,15", None, False),
    ("26.07.2026", [[f"Ödeme {SOFT} Teşekkür Ederiz {SOFT}"]], "+7.645,30", None, False),
    ("21.07.2026", [["5550000001 ÖRNEKTEL FATURA ÖDEME"]], "412,35", None, False),
    ("18.07.2026", [["16/06 ÖRNEK MOBİLYA 42.KON KONYA"]], "1.145,00", ["(6.870,00 TL İşlemin 2/6", "Taksidi)"], False),
    ("15.07.2026", [["15/07 DENEME ELEKTRONİK 42.KON KONYA"]], "3.150,00", ["(9.450,00 TL İşlemin 1/3", "Taksidi)"], False),
    ("14.07.2026", [["DENEME KİTAPEVİ", "ESKİŞEHİR"]], "263,40", None, True),
    ("10.07.2026", [["12/04 ÖRNEK BEYAZ EŞYA ANTALYA"]], "2.135,00", ["(12.810,00 TL İşlemin 4/6", "Taksidi)"], False),
    ("07.07.2026", [["DENEME GİYİM İADE KONYA"]], "+137,60", None, False),
    ("03.07.2026", [["ÖRNEK ÇOK UZUN ADLI İNTERNET"], ["MAĞAZACILIK HİZMETLERİ KONYA"]], "1.087,25", None, False),
    ("01.07.2026", [["ÖRNEK FIRIN KONYA"]], "46,50", None, False),
    ("01.07.2026", [["ÖRNEK FIRIN KONYA"]], "46,50", None, False),
]
MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]

# Turkish letters missing from WinAnsi, put on codes whose WinAnsi letters are not used here
EXTRA = {"Ğ": (0xC0, "Gbreve"), "ğ": (0xC1, "gbreve"), "İ": (0xC2, "Idotaccent"),
         "ı": (0xC3, "dotlessi"), "Ş": (0xC4, "Scedilla"), "ş": (0xC5, "scedilla")}
WIDTH, BLOCK, TOP, BOTTOM = 1024, 100, 300, 260
SCALE = 0.581055


def encode(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        if ch in EXTRA:
            out.append(EXTRA[ch][0])
        else:
            out += ch.encode("cp1252")
    escaped = bytes(out).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + escaped + b")"


def piece(x: float, y: float, text: str) -> bytes:
    return b"BT /F1 10 Tf 1 0 0 1 %.1f %.1f Tm " % (x, y) + encode(text) + b" Tj ET\n"


def form_content(height: int) -> bytes:
    out = bytearray()
    out += piece(13, height - 86, "Sayın DENEME KULLANICI")
    out += piece(13, height - 108, "Kart Hareketleri ekranında son işlemleriniz listelenmektedir")
    y0 = height - TOP + 100
    for k, (day, desc, amount, note, buttons) in enumerate(CARD_LINES):
        y = y0 - k * BLOCK
        dd, mm, yyyy = day.split(".")
        out += piece(519, y, amount)
        out += piece(519 + 6 * len(amount), y, "TL")
        offset = 19 if note or len(desc) > 1 else 26
        for row, pieces in enumerate(desc):
            x = 115
            for text in pieces:
                out += piece(x, y - offset - 12 * row, text)
                x += 6 * len(text) + 4
        out += piece(54, y - 35, f"{int(dd)} {MONTHS[int(mm) - 1]}")
        if note:
            out += piece(115, y - 37, note[0])
            out += piece(115 + 6 * len(note[0]) + 4, y - 37, note[1])
        out += piece(58, y - 48, yyyy)
        if buttons:
            out += piece(760, y - 49, "TAKSİTLENDİR")
            out += piece(858, y - 49, "ERTELE")
    foot = y0 - len(CARD_LINES) * BLOCK - 40
    out += piece(13, foot, "Bu belge örnek amaçlıdır; içindeki hiçbir bilgi gerçek değildir.")
    out += piece(13, foot - 13, "www.ziraatbank.com.tr")
    return bytes(out)


def stream(dictionary: bytes, data: bytes) -> bytes:
    return dictionary[:-2] + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def write_pdf(path: Path) -> None:
    height = TOP + len(CARD_LINES) * BLOCK + BOTTOM
    differences = b" ".join(b"%d /%s" % (code, name.encode()) for code, name in sorted(EXTRA.values()))
    top = 832 - height * SCALE
    pages = [top + k * 827 for k in range(int(height * SCALE // 827) + 1)]
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % (5 + 2 * k) for k in range(len(pages))) + b"] /Count %d >>" % len(pages),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /Type /Encoding "
        b"/BaseEncoding /WinAnsiEncoding /Differences [" + differences + b"] >> >>",
        4: stream(
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R >> >> >>" % (WIDTH, height),
            form_content(height),
        ),
    }
    for k, ty in enumerate(pages):
        content = b"0 5 595 827 re W n\nq 0 5 595 827 re W n %.6f 0 0 %.6f 0 %.4f cm /Fm1 Do Q\n" % (SCALE, SCALE, ty)
        objects[6 + 2 * k] = stream(b"<< >>", content)
        objects[5 + 2 * k] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Fm1 4 0 R >> >> "
            b"/Contents %d 0 R >>" % (6 + 2 * k)
        )
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    size = max(objects) + 1
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    path.write_bytes(bytes(out))


if __name__ == "__main__":
    write_pdf(HERE / "ziraat_card.pdf")
    print(f"written: {len(CARD_LINES)} lines")
