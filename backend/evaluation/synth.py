"""Synthetic labelled invoice generator: digital PDFs (3 layouts), degraded scans, images, TIFF, XLSX, CSV,
injected business errors, duplicates, cross-source pairs and corrupted files. Ground truth -> labels.json."""
import datetime as dt
import io
import json
import random
from pathlib import Path

import numpy as np
import openpyxl
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

try:
    import pymupdf as fitz
except ImportError:
    import fitz

SUPPLIERS = ["Northwind Traders Ltd", "Blue Harbor Logistics", "Apex Office Supplies", "Greenfield Foods Inc", "Delta Engineering Works",
             "Summit Medical Supply", "Orion Digital Services", "Redwood Packaging Co", "Silverline Electronics", "Harbor Point Consulting",
             "Crescent Textiles Ltd", "Pioneer Hardware", "Lakeside Catering Services", "Vertex Software Solutions", "Maple Leaf Stationers"]
CUSTOMERS = ["Globex Corporation", "Initech Holdings", "Umbrella Trading Co", "Stark Manufacturing", "Wayne Enterprises",
             "Hooli Technologies", "Wonka Industries", "Soylent Distribution", "Acme Retail Group", "Cyberdyne Systems"]
ITEMS = ["Steel bolts (box)", "Office chair", "Laptop stand", "Cardboard cartons", "LED panel light", "Printer toner", "Safety gloves",
         "Consulting hour", "Cloud hosting plan", "Network switch", "Packing tape roll", "Copper wire spool", "USB-C cable",
         "Whiteboard markers", "Maintenance service"]
CUR = {1: "USD", 2: "EUR", 3: "BDT"}
RATE = {1: 10, 2: 15, 3: 5}
FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency", "line_items"]


def make_invoice(rng: random.Random, tpl: int, inject: str | None = None, year: int = 2025) -> dict:
    items = []
    for desc in rng.sample(ITEMS, rng.randint(2, 6)):
        q, p = rng.randint(1, 12), round(rng.uniform(4, 480), 2)
        items.append({"description": desc, "quantity": float(q), "unit_price": p, "amount": round(q * p, 2)})
    sub = round(sum(i["amount"] for i in items), 2)
    tax = round(sub * RATE[tpl] / 100, 2)
    total = round(sub + tax, 2)
    if inject == "bad_total":
        total = round(total + rng.uniform(5, 50), 2)        # the document itself is wrong; truth = what is printed
    num = {1: f"INV-{year}-{rng.randint(1, 9999):04d}", 2: str(rng.randint(10000, 99999)), 3: f"SI/{year}/{rng.randint(1, 99999):05d}"}[tpl]
    return {"invoice_number": num, "invoice_date": None if inject == "no_date" else dt.date(year, rng.randint(1, 12), rng.randint(13, 28)).isoformat(),
            "supplier_name": rng.choice(SUPPLIERS), "customer_name": rng.choice(CUSTOMERS), "subtotal": sub, "tax": tax, "total": total,
            "currency": CUR[tpl], "line_items": items, "_tpl": tpl, "_rate": RATE[tpl]}


def _fmt_date(inv):
    d = inv["invoice_date"]
    if not d:
        return None
    d = dt.date.fromisoformat(d)
    return {1: d.isoformat(), 2: d.strftime("%d %B %Y"), 3: d.strftime("%d/%m/%Y")}[inv["_tpl"]]


def _n(v):
    return f"{v:,.2f}"


def _rows(c, inv, y, cols, index=False):
    c.setFont("Helvetica", 10)
    for k, it in enumerate(inv["line_items"], 1):
        if index:
            c.drawString(cols[0], y, str(k))
        c.drawString(cols[1], y, it["description"])
        c.drawRightString(cols[2], y, str(int(it["quantity"])))
        c.drawRightString(cols[3], y, _n(it["unit_price"]))
        c.drawRightString(cols[4], y, _n(it["amount"]))
        y -= 16
    return y


def draw_invoice(c, inv):
    t, date = inv["_tpl"], _fmt_date(inv)
    if t == 1:
        c.setFont("Helvetica-Bold", 16); c.drawString(50, 800, inv["supplier_name"])
        c.setFont("Helvetica", 9); c.drawString(50, 786, "12 Market Street, Springfield"); c.drawString(50, 775, "Tax ID: 12-3456789")
        c.setFont("Helvetica-Bold", 22); c.drawString(50, 740, "INVOICE")
        c.setFont("Helvetica", 10); c.drawString(50, 715, f"Invoice No: {inv['invoice_number']}")
        if date:
            c.drawString(50, 700, f"Invoice Date: {date}")
            c.drawString(50, 685, f"Due Date: {(dt.date.fromisoformat(inv['invoice_date']) + dt.timedelta(days=30)).isoformat()}")
        c.drawString(50, 660, "PO Number: PO-7788")
        c.drawString(50, 640, "Bill To:"); c.drawString(50, 626, inv["customer_name"]); c.drawString(50, 614, "99 Industrial Way")
        c.setFont("Helvetica-Bold", 10); c.drawString(50, 585, "Description")
        for x, s in ((330, "Qty"), (430, "Unit Price"), (540, "Amount")):
            c.drawRightString(x, 585, s)
        y = _rows(c, inv, 568, (0, 50, 330, 430, 540))
        c.setFont("Helvetica", 10)
        for lab, v in (("Subtotal:", inv["subtotal"]), (f"Tax ({inv['_rate']}%):", inv["tax"]), ("Total:", inv["total"])):
            y -= 4; c.drawString(380, y - 12, lab); c.drawRightString(540, y - 12, f"${_n(v)}"); y -= 14
    elif t == 2:
        c.setFont("Helvetica-Bold", 15); c.drawString(50, 800, inv["supplier_name"])
        c.setFont("Helvetica", 10); c.drawString(380, 800, f"Invoice #: {inv['invoice_number']}")
        if date:
            c.drawString(380, 785, f"Date of Issue: {date}")
        c.drawString(50, 770, "VAT No: DE123456789")
        c.drawString(50, 730, "Billed To:"); c.drawString(380, 730, "Currency: EUR"); c.drawString(50, 716, inv["customer_name"])
        c.setFont("Helvetica-Bold", 10); c.drawString(50, 670, "Item")
        for x, s in ((330, "Quantity"), (430, "Price"), (540, "Total")):
            c.drawRightString(x, 670, s)
        y = _rows(c, inv, 653, (0, 50, 330, 430, 540))
        c.setFont("Helvetica", 10)
        for lab, v in (("Subtotal", inv["subtotal"]), (f"VAT ({inv['_rate']}%)", inv["tax"]), ("Grand Total", inv["total"])):
            y -= 4; c.drawString(380, y - 12, lab); c.drawRightString(540, y - 12, f"€{_n(v)}"); y -= 14
    else:
        c.setFont("Helvetica-Bold", 18); c.drawCentredString(297, 800, "TAX INVOICE")
        c.setFont("Helvetica", 10)
        c.drawString(50, 770, f"Supplier: {inv['supplier_name']}"); c.drawString(50, 755, f"Customer: {inv['customer_name']}")
        c.drawString(50, 740, f"Invoice Number: {inv['invoice_number']}")
        if date:
            c.drawString(50, 725, f"Issued: {date}")
        c.drawString(50, 710, "Currency: BDT"); c.drawString(50, 695, "VAT Reg No: 001234567")
        c.setFont("Helvetica-Bold", 10); c.drawString(50, 660, "No."); c.drawString(90, 660, "Description")
        for x, s in ((330, "Qty"), (430, "Rate"), (540, "Amount")):
            c.drawRightString(x, 660, s)
        y = _rows(c, inv, 643, (50, 90, 330, 430, 540), index=True)
        c.setFont("Helvetica", 10)
        for lab, v in (("Net Amount", inv["subtotal"]), (f"VAT ({inv['_rate']}%)", inv["tax"]), ("Total Due", inv["total"])):
            y -= 4; c.drawString(380, y - 12, lab); c.drawRightString(540, y - 12, _n(v)); y -= 14
    c.setFont("Helvetica", 8); c.drawString(50, 40, "Thank you for your business. Payment terms: net 30 days.")
    c.showPage()


def render_pdf(path, invs):
    c = canvas.Canvas(str(path), pagesize=A4)
    for inv in invs:
        draw_invoice(c, inv)
    c.save()


def pdf_to_images(path, dpi=200):
    doc = fitz.open(str(path))
    out = [Image.frombytes("RGB", (p.width, p.height), p.samples) for p in (pg.get_pixmap(dpi=dpi, alpha=False) for pg in doc)]
    doc.close()
    return out


def degrade(img: Image.Image, rng: random.Random, level: str = "poor") -> Image.Image:
    rot, blur, sigma, q = (rng.uniform(-0.5, 0.5), 0.3, 4, 85) if level == "good" else (rng.uniform(-2.5, 2.5), 0.9, 14, 55)
    img = img.rotate(rot, expand=False, fillcolor=(255, 255, 255), resample=Image.BICUBIC).filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(img).astype(np.float32)
    if level != "good":
        a = 255 - (255 - a) * 0.8                                  # faded print
    a += np.random.default_rng(rng.randint(0, 10**6)).normal(0, sigma, a.shape)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=q); buf.seek(0)
    return Image.open(buf).convert("RGB")


def save_image_pdf(imgs, path):
    doc = fitz.open()
    for im in imgs:
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(str(path)); doc.close()


def write_xlsx(path, invs, date_as_text=False):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Invoices"
    ws.append(["Monthly invoice register"]); ws.append([])
    ws.append(["Invoice No", "Invoice Date", "Supplier", "Customer", "Currency", "Description", "Qty", "Unit Price", "Line Total", "Subtotal", "Tax", "Total"])
    for inv in invs:
        d = dt.date.fromisoformat(inv["invoice_date"])
        for it in inv["line_items"]:
            ws.append([inv["invoice_number"], d.strftime("%d/%m/%Y") if date_as_text else dt.datetime(d.year, d.month, d.day),
                       inv["supplier_name"], inv["customer_name"], inv["currency"], it["description"], it["quantity"], it["unit_price"],
                       it["amount"], inv["subtotal"], inv["tax"], inv["total"]])
    n = wb.create_sheet("Notes"); n.append(["Prepared by finance team"]); n.append(["Do not edit"])
    wb.create_sheet("Empty")
    wb.save(str(path))


def write_xls(path, invs):
    """Legacy Excel 97-2003 (.xls, BIFF) workbook with the same layout as write_xlsx. Needs xlwt (dev-only)."""
    import xlwt
    wb = xlwt.Workbook(); ws = wb.add_sheet("Invoices")
    date_fmt = xlwt.easyxf(num_format_str="DD/MM/YYYY")
    ws.write(0, 0, "Legacy invoice register")
    hdr = ["Invoice No", "Invoice Date", "Supplier", "Customer", "Currency", "Description", "Qty", "Unit Price", "Line Total", "Subtotal", "Tax", "Total"]
    for c, h in enumerate(hdr):
        ws.write(2, c, h)
    r = 3
    for inv in invs:
        d = dt.date.fromisoformat(inv["invoice_date"])
        for it in inv["line_items"]:
            row = [inv["invoice_number"], None, inv["supplier_name"], inv["customer_name"], inv["currency"], it["description"],
                   it["quantity"], it["unit_price"], it["amount"], inv["subtotal"], inv["tax"], inv["total"]]
            for c, v in enumerate(row):
                if c == 1:
                    ws.write(r, c, dt.datetime(d.year, d.month, d.day), date_fmt)
                else:
                    ws.write(r, c, v)
            r += 1
    wb.add_sheet("Notes").write(0, 0, "Prepared by finance team")
    wb.save(str(path))


def write_csv(path, invs):
    lines = ["Invoice Number,Date,Vendor,Client,Subtotal,VAT,Grand Total,Currency"]
    for inv in invs:
        d = dt.date.fromisoformat(inv["invoice_date"]).strftime("%d/%m/%Y")
        lines.append(f'{inv["invoice_number"]},{d},"{inv["supplier_name"]}","{inv["customer_name"]}",{inv["subtotal"]},{inv["tax"]},{inv["total"]},{inv["currency"]}')
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def truth(inv, csv=False):
    t = {k: inv[k] for k in FIELDS}
    if csv:
        t["line_items"] = []
    return t


def build_dataset(out: Path, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    labels: list[dict] = []

    def add(name, kind, invs, flags=None, expect="ok", csv=False):
        labels.append({"file": name, "kind": kind, "expect": expect,
                       "invoices": [{"fields": truth(i, csv), "flags": sorted((flags or [[]] * len(invs))[k])} for k, i in enumerate(invs)]})

    clean = []
    for k in range(12):                                         # A: digital PDFs
        inv = make_invoice(rng, k % 3 + 1); clean.append(inv)
        render_pdf(out / f"digital_{k:02d}.pdf", [inv]); add(f"digital_{k:02d}.pdf", "pdf_digital", [inv])
    for k in range(2):                                          # B: multi-invoice PDFs
        invs = [make_invoice(rng, (k + j) % 3 + 1) for j in range(5)]
        render_pdf(out / f"batch_{k}.pdf", invs); add(f"batch_{k}.pdf", "pdf_multi_invoice", invs)
    for k in range(5):                                          # C: scanned PDFs (image-only)
        inv = make_invoice(rng, k % 3 + 1); tmp = out / "_t.pdf"; render_pdf(tmp, [inv])
        save_image_pdf([degrade(im, rng, "good") for im in pdf_to_images(tmp, 200)], out / f"scan_{k}.pdf"); add(f"scan_{k}.pdf", "pdf_scanned", [inv])
    for k in range(4):                                          # D: PNG, good quality
        inv = make_invoice(rng, k % 3 + 1); tmp = out / "_t.pdf"; render_pdf(tmp, [inv])
        degrade(pdf_to_images(tmp, 200)[0], rng, "good").save(out / f"photo_good_{k}.png"); add(f"photo_good_{k}.png", "image_good", [inv])
    for k in range(5):                                          # E: JPG, poor quality
        inv = make_invoice(rng, k % 3 + 1); tmp = out / "_t.pdf"; render_pdf(tmp, [inv])
        degrade(pdf_to_images(tmp, 150)[0], rng, "poor").save(out / f"photo_poor_{k}.jpg", quality=55); add(f"photo_poor_{k}.jpg", "image_poor", [inv])
    invs = [make_invoice(rng, j + 1) for j in range(3)]         # F: multipage TIFF
    tmp = out / "_t.pdf"; render_pdf(tmp, invs)
    ims = [degrade(im, rng, "good") for im in pdf_to_images(tmp, 200)]
    ims[0].save(out / "multipage.tiff", save_all=True, append_images=ims[1:]); add("multipage.tiff", "image_tiff_multipage", invs)
    for k in range(3):                                          # H: XLSX, multi-sheet
        invs = [make_invoice(rng, rng.randint(1, 3)) for _ in range(4)]
        write_xlsx(out / f"register_{k}.xlsx", invs, date_as_text=(k == 2)); add(f"register_{k}.xlsx", "xlsx", invs)
    for k in range(3):                                          # I: CSV
        invs = [make_invoice(rng, rng.randint(1, 3)) for _ in range(5)]
        write_csv(out / f"export_{k}.csv", invs); add(f"export_{k}.csv", "csv", invs, csv=True)
    for k in range(3):                                          # J: injected total mismatch
        inv = make_invoice(rng, k % 3 + 1, "bad_total"); render_pdf(out / f"badtotal_{k}.pdf", [inv])
        add(f"badtotal_{k}.pdf", "inject_total_mismatch", [inv], [["total_mismatch"]])
    for k in range(2):                                          # missing date -> must stay null
        inv = make_invoice(rng, k + 1, "no_date"); render_pdf(out / f"nodate_{k}.pdf", [inv])
        add(f"nodate_{k}.pdf", "inject_missing_field", [inv], [["missing_field"]])
    for k in range(2):                                          # exact duplicates of earlier files
        render_pdf(out / f"dup_{k}.pdf", [clean[k]]); add(f"dup_{k}.pdf", "inject_duplicate", [clean[k]], [["duplicate_invoice"]])
    mism = dict(clean[2]); mism["tax"] = round(mism["tax"] + 5, 2); mism["total"] = round(mism["total"] + 5, 2)
    write_xlsx(out / "xsource_mismatch.xlsx", [mism]); add("xsource_mismatch.xlsx", "inject_cross_source_mismatch", [mism], [["cross_source_mismatch"]])
    write_csv(out / "xsource_match.csv", [clean[3]]); add("xsource_match.csv", "inject_cross_source_match", [clean[3]], [["cross_verified"]], csv=True)
    good = (out / "digital_00.pdf").read_bytes()                # K: corrupted files
    (out / "corrupt_truncated.pdf").write_bytes(good[:300]); add("corrupt_truncated.pdf", "corrupted", [], expect="fail")
    (out / "corrupt_garbage.pdf").write_bytes(b"%PDF-1.7\n" + bytes(rng.randrange(256) for _ in range(3000))); add("corrupt_garbage.pdf", "corrupted", [], expect="fail")
    png = (out / "photo_good_0.png").read_bytes()
    (out / "corrupt_truncated.png").write_bytes(png[:1500]); add("corrupt_truncated.png", "corrupted", [], expect="fail")
    (out / "corrupt_book.xlsx").write_bytes(b"PK\x03\x04" + bytes(rng.randrange(256) for _ in range(2000))); add("corrupt_book.xlsx", "corrupted", [], expect="fail")
    (out / "_t.pdf").unlink(missing_ok=True)
    (out / "labels.json").write_text(json.dumps(labels, indent=1))
    return labels


if __name__ == "__main__":  # python -m evaluation.synth evaluation/data
    import sys
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "evaluation/data")
    print(f"wrote {len(build_dataset(dest))} files + labels.json to {dest}")
