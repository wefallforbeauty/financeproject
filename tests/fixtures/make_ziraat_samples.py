"""Write made-up Ziraat Bankası statements in the bank's two download formats.

The layout copies a real export (October 2026) cell for cell, with invented
names, numbers and transactions. The .xlsx file reproduces the quirks of the
bank's generator: an ``x:`` namespace prefix, no cell references, text cells
of type ``str``, an empty ``<fill/>`` in the style sheet (which openpyxl
rejects) and stray spaces in descriptions. The .html file starts with a
bot-protection script, as the real download does.

Run ``python tests/fixtures/make_ziraat_samples.py`` to rewrite both files.
"""

from __future__ import annotations

import zipfile
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
PERIOD = ("01.09.2026", "30.09.2026")
OPENING = Decimal("12500.00")
CARD = "KART NO: 4000 **** **** 0000"

# date, receipt number, description, amount as the .xlsx stores it (oldest first)
LINES = [
    ("01.09.2026", "F01001", "MAAŞ ÖDEMESİ ÖRNEK A.Ş.", "45000"),
    ("03.09.2026", "F03112", f"POS ALIŞVERİŞ {CARD} İŞYERİ: MIGROS KONYA MUTABAKAT: 1234567", "-1234.56"),
    ("05.09.2026", "F05220", "KİRA ÖDEMESİ EFT ÖRNEK EMLAK", "-15000"),
    ("10.09.2026", "F10301", "TURKCELL FATURA ÖDEMESİ", "-389.9"),
    ("15.09.2026", "F15404", f"SANAL POS ALIŞVERİŞ {CARD} İŞYERİ: NETFLIX.COM MUTABAKAT: 7654321", "-229.99"),
    ("15.09.2026", "F15405", f"POS ALIŞVERİŞ (YURTİÇİ) {CARD} İŞYERİ: BIM A.S. U123  MUTABAKAT: 1111111", "-85.50"),
    ("15.09.2026", "F15406", f"POS ALIŞVERİŞ (YURTİÇİ) {CARD} İŞYERİ: BIM A.S. U123  MUTABAKAT: 1111112 ", "-85.50"),
    ("20.09.2026", "F20507", " ALİ VELİ Ziraat Mobil Havale", "-2000.00"),
    ("25.09.2026", "F25608", "VADELİ HESAPTAN AKTARIM", "5000"),
    ("28.09.2026", "F28709", f"POS ALIŞVERİŞ {CARD} İŞYERİ: OPET AKARYAKIT MUTABAKAT: 2222222", "-1750"),
]


def with_balances():
    balance = OPENING
    out = []
    for day, receipt, text, amount in LINES:
        balance += Decimal(amount)
        out.append((day, receipt, text, Decimal(amount), balance))
    return out


def turkish(value: Decimal) -> str:
    """-1234.5 → '-1.234,50'."""
    body = f"{abs(value):,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")
    return ("-" if value < 0 else "") + body


def totals():
    debit = sum((Decimal(a) for *_, a in LINES if Decimal(a) < 0), Decimal(0))
    credit = sum((Decimal(a) for *_, a in LINES if Decimal(a) > 0), Decimal(0))
    return debit, credit


NS = 'xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
DECL = '﻿<?xml version="1.0" encoding="utf-8"?>'


def text_cell(value: str, style: int = 0) -> str:
    return f'<x:c s="{style}" t="str"><x:v>{escape(value)}</x:v></x:c>'


def number_cell(value: str, style: int = 2) -> str:
    return f'<x:c s="{style}" t="n"><x:v>{value}</x:v></x:c>'


def row(*cells: str) -> str:
    return "<x:row>" + "".join(cells) + "</x:row>" if cells else "<x:row />"


def write_xlsx(path: Path) -> None:
    debit, credit = totals()
    rows = [row(), row(), row(), row()]
    rows.append(row(text_cell("Sayın ÖRNEK MÜŞTERİ"), '<x:c s="0" />'))
    rows.append(row(text_cell(f"{PERIOD[0]} - {PERIOD[1]} tarihleri arasındaki hesap hareketleri listelenmektedir."), '<x:c s="0" />'))
    rows.append(row(text_cell("Hesap Numarası"), '<x:c s="0" />', text_cell("0000-000000000-0000 ÖRNEK ŞUBESİ TL")))
    rows.append(row(text_cell("IBAN"), '<x:c s="0" />', text_cell("TR000000000000000000000000")))
    rows += [row(), row(), row(text_cell("Hesap Hareketleri", 1))]
    rows.append(row(*(text_cell(h, 1) for h in ("Tarih", "Fiş No", "Açıklama", "İşlem Tutarı", "Bakiye"))))
    for day, receipt, text, _amount, balance in reversed(with_balances()):  # newest first
        raw = next(a for d, r, _, a in LINES if (d, r) == (day, receipt))
        rows.append(row(text_cell(day), text_cell(receipt), text_cell(text), number_cell(raw), number_cell(f"{balance:.2f}")))
    rows.append(row(*['<x:c s="0" />'] * 5))
    rows.append(
        row(*['<x:c s="0" />'] * 3, text_cell(f"Borç:{turkish(debit)}", 1), text_cell(f"Alacak:{turkish(credit).replace(',00', '')}", 1))
    )
    rows += [row(), row()]
    rows.append(row(text_cell("Bu belge örnek amaçlıdır; içindeki hiçbir bilgi gerçek değildir."), '<x:c s="0" />'))
    rows.append(row(text_cell("www.ziraatbank.com.tr"), '<x:c s="0" />'))
    sheet = (
        f"{DECL}<x:worksheet {NS}><x:cols>"
        '<x:col min="1" max="2" width="42" customWidth="1" /><x:col min="3" max="4" width="96" customWidth="1" />'
        f"</x:cols><x:sheetData>{''.join(rows)}</x:sheetData></x:worksheet>"
    )
    styles = (
        f"{DECL}<x:styleSheet {NS}><x:numFmts>"
        '<x:numFmt numFmtId="7732" formatCode="#,##0.##" /><x:numFmt numFmtId="7733" formatCode="#,##0" /></x:numFmts>'
        "<x:fonts><x:font /><x:font><x:b /></x:font></x:fonts><x:fills><x:fill /></x:fills>"
        "<x:borders><x:border /></x:borders><x:cellXfs>"
        '<x:xf fontId="0" fillId="0" borderId="0" /><x:xf fontId="1" />'
        '<x:xf numFmtId="7732" fontId="0" applyNumberFormat="1" /><x:xf numFmtId="7732" fontId="1" applyNumberFormat="1" />'
        "</x:cellXfs></x:styleSheet>"
    )
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    parts = {
        "[Content_Types].xml": (
            f'{DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml" />'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml" />'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml" />'
            '<Override PartName="/xl/worksheets/sheet.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml" />'
            "</Types>"
        ),
        "_rels/.rels": (
            f'{DECL}<Relationships xmlns="{pkg}">'
            f'<Relationship Type="{rel}/officeDocument" Target="/xl/workbook.xml" Id="R1" /></Relationships>'
        ),
        "xl/workbook.xml": (
            f'{DECL}<x:workbook {NS}><x:sheets><x:sheet name="Hesap hareketleri" sheetId="1" r:id="R3" '
            f'xmlns:r="{rel}" /></x:sheets></x:workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'{DECL}<Relationships xmlns="{pkg}">'
            f'<Relationship Type="{rel}/styles" Target="/xl/styles.xml" Id="R2" />'
            f'<Relationship Type="{rel}/worksheet" Target="/xl/worksheets/sheet.xml" Id="R3" /></Relationships>'
        ),
        "xl/styles.xml": styles,
        "xl/worksheets/sheet.xml": sheet,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, content.encode("utf-8"))


TD = 'style="word-wrap:break-word;text-align:left;vertical-align:center;padding: 3px;width:10%"'


def write_html(path: Path) -> None:
    debit, credit = totals()
    junk = (
        '<script type="text/javascript">\r\n(function(){var l=-1,t="<table><tr><td>not data</td></tr></table>";'
        "if(l<0&&t.length>3){window.x=t}})();\r\n</script>"
    )
    identity = (
        '<table class="tableIdentity" width="100%">'
        '<tr><td rowspan="2">Sayın</td><td>:</td><td>ÖRNEK MÜŞTERİ</td><td>Şube Kodu</td><td>:</td><td>ÖRNEK ŞUBESİ</td></tr>'
        '<tr><td colspan="2"></td><td>Müşteri/Hesap No</td><td>:</td><td>000000000-0000</td></tr>'
        '<tr><td rowspan="3">Adres</td><td rowspan="3">:</td><td rowspan="3">ÖRNEK MAH. ÖRNEK SK. NO: 1</td>'
        "<td>IBAN</td><td>:</td><td>TR000000000000000000000000</td></tr>"
        "<tr><td>Döviz Cinsi</td><td>:</td><td>TRY</td></tr>"
        f"<tr><td>Dönem</td><td>:</td><td>{PERIOD[0]}-{PERIOD[1]}</td></tr></table>"
    )
    head = "".join(f"<th {TD}><b>{h}</b></th>" for h in ("Tarih", "Fiş No", "Açıklama", "Tutar", "Bakiye"))
    body = "".join(
        f"<tr><td {TD}>{day}</td><td {TD}>{receipt}</td><td {TD}>{escape(text.strip())}</td>"
        f"<td {TD}>{turkish(amount)}</td><td {TD}>{turkish(balance)}</td></tr>\r\n"
        for day, receipt, text, amount, balance in reversed(with_balances())
    )
    foot = (
        f"<tr><td {TD}></td><td {TD}>Borç:</td><td {TD}></td><td {TD}>{turkish(debit)}</td><td {TD}></td></tr>"
        f"<tr><td {TD}></td><td {TD}>Alacak:</td><td {TD}></td><td {TD}>{turkish(credit)}</td><td {TD}></td></tr>"
    )
    page = (
        f"<html><head>\r\n<APM_DO_NOT_TOUCH>\r\n{junk}\r\n</APM_DO_NOT_TOUCH>\r\n"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
        "<style>td { font-family: Arial; }</style></head><body>"
        '<div><table width="100%"><tr><td align="left"><img src="logo.png"></td></tr></table></div>'
        f"<div>{identity}</div><br><div><table class=\"tableIdentity\" width=\"100%\"><tr>{head}</tr>\r\n{body}{foot}</table></div>"
        "<div>Bu belge örnek amaçlıdır; içindeki hiçbir bilgi gerçek değildir.</div></body></html>"
    )
    path.write_text(page, encoding="utf-8", newline="")


if __name__ == "__main__":
    write_xlsx(HERE / "ziraat_statement.xlsx")
    write_html(HERE / "ziraat_statement.html")
    debit, credit = totals()
    print(f"written; opening {OPENING}, closing {with_balances()[-1][4]}, debit {debit}, credit {credit}")
