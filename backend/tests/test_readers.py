import pytest
from app.processing.ocr import ocr_available
from app.processing.readers import (CorruptFileError, UnsupportedFileError, read_image, read_pdf, read_spreadsheet, sniff_type)
from app.extraction.tabular import extract_table

needs_ocr = pytest.mark.skipif(not ocr_available(), reason="tesseract not installed")


def test_digital_pdf_uses_text_layer_not_ocr(files):
    path, inv = files["digital"]
    pages = read_pdf(path)
    assert [p.method for p in pages] == ["text"] and inv["invoice_number"] in pages[0].text
    # table rows keep cells on one line
    first = inv["line_items"][0]
    assert any(first["description"] in l and f'{first["amount"]:,.2f}' in l for l in pages[0].text.splitlines())


@needs_ocr
def test_scanned_pdf_falls_back_to_ocr(files):
    path, inv = files["scanned"]
    p = read_pdf(path)[0]
    assert p.method == "ocr" and p.ocr_conf > 50 and inv["invoice_number"] in p.text


@needs_ocr
def test_poor_quality_image_still_reads(files):
    path, inv = files["poor"]
    text = read_image(path)[0].text
    assert "INVOICE" in text.upper() and inv["supplier_name"].split()[0].lower() in text.lower()


def test_multipage_pdf(files):
    path, invs = files["batch"]
    pages = read_pdf(path)
    assert len(pages) == 3 and all(i["invoice_number"] in p.text for i, p in zip(invs, pages))


@needs_ocr
def test_multipage_tiff_pages_are_distinct(tmp_path, files):
    from evaluation import synth
    pdf, invs = files["batch"]
    ims = synth.pdf_to_images(pdf, 150)
    ims[0].save(tmp_path / "m.tiff", save_all=True, append_images=ims[1:])
    pages = read_image(tmp_path / "m.tiff")
    assert len(pages) == 3 and len({p.text for p in pages}) == 3      # regression: frames must not alias


def test_corrupt_files_raise_clean_errors(files):
    with pytest.raises(CorruptFileError):
        read_pdf(files["broken_pdf"])
    with pytest.raises(CorruptFileError):
        read_image(files["broken_png"])


def test_sniff_rejects_spoofed_and_unknown_types():
    assert sniff_type(b"%PDF-1.4 ...", "a.pdf") == "pdf"
    with pytest.raises(UnsupportedFileError):
        sniff_type(b"MZ\x90\x00 exe", "invoice.pdf")           # exe renamed to .pdf
    with pytest.raises(UnsupportedFileError):
        sniff_type(b"hello", "notes.txt")
    with pytest.raises(UnsupportedFileError):
        sniff_type(b"PK\x03\x04", "x.png")                   # zip bytes renamed to .png


def test_xlsx_multiple_sheets_and_line_item_grouping(files):
    path, invs = files["xlsx"]
    sheets = read_spreadsheet(path, "xlsx")
    assert [s.name for s in sheets] == ["Invoices", "Notes"]          # empty sheet dropped
    recs = extract_table(sheets[0].df, "Invoices")
    assert len(recs) == 3
    r0 = recs[0]["fields"]
    assert r0["invoice_number"] == invs[0]["invoice_number"] and len(r0["line_items"]) == len(invs[0]["line_items"])
    assert r0["total"] == invs[0]["total"] and r0["invoice_date"] == invs[0]["invoice_date"]
    assert extract_table(sheets[1].df, "Notes") == []                 # non-invoice sheet -> nothing invented


def test_legacy_xls_workbook(tmp_path):
    pytest.importorskip("xlwt")
    import random
    from evaluation import synth
    rng = random.Random(11)
    invs = [synth.make_invoice(rng, 1) for _ in range(2)]
    p = tmp_path / "legacy.xls"
    synth.write_xls(p, invs)
    assert sniff_type(p.read_bytes()[:8], "legacy.xls") == "xls"
    sheets = read_spreadsheet(p, "xls")
    assert [s.name for s in sheets] == ["Invoices", "Notes"]
    recs = extract_table(sheets[0].df, "Invoices")
    assert [r["fields"]["invoice_number"] for r in recs] == [i["invoice_number"] for i in invs]
    f = recs[1]["fields"]
    assert f["total"] == invs[1]["total"] and f["invoice_date"] == invs[1]["invoice_date"]
    assert len(f["line_items"]) == len(invs[1]["line_items"])


def test_csv_with_header_aliases(files):
    path, invs = files["csv"]
    recs = extract_table(read_spreadsheet(path, "csv")[0].df, "CSV")
    assert [r["fields"]["invoice_number"] for r in recs] == [i["invoice_number"] for i in invs]
    assert recs[1]["fields"]["tax"] == invs[1]["tax"] and recs[1]["fields"]["supplier_name"] == invs[1]["supplier_name"]


def test_corrupt_spreadsheet(tmp_path):
    p = tmp_path / "x.xlsx"; p.write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(CorruptFileError):
        read_spreadsheet(p, "xlsx")
    e = tmp_path / "e.csv"; e.write_text("")
    with pytest.raises(CorruptFileError):
        read_spreadsheet(e, "csv")
