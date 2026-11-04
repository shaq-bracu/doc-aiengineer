"""Regressions from the 1000-page scan: OCR reads SI/ as S1/ or S!/, which silently produced wrong numbers and merged pages."""
from types import SimpleNamespace

import pytest

from app.extraction import extractor, rules
from app.extraction.utils import fold_lookalikes, has_lookalike
from app.pipeline import MIN_CLEAN_READS, reconcile_lookalike_numbers, segment_pages
from app.processing.readers import Page
from app.validation import check_fields, dedupe_key

TAX_INVOICE = "TAX INVOICE\nSupplier: Acme Ltd\nInvoice Number: {}\nTotal Due 5.00"


@pytest.mark.parametrize("value", ["S1/2025/00020", "S1I/2025/7", "2O25-0001", "1NV-2025-7", "00I2"])
def test_lookalike_numbers_are_recognised(value):
    assert has_lookalike(value)


@pytest.mark.parametrize("value", ["SI/2025/00020", "INV-2025-0001", "20073", "INV0012", "INV001", "PO-7788", "Q3-100"])
def test_ordinary_numbers_are_not_flagged(value):
    assert not has_lookalike(value)


def test_folding_makes_lookalikes_compare_equal():
    assert fold_lookalikes("S1/2025") == fold_lookalikes("SI/2025") == fold_lookalikes("S!/2025")
    assert dedupe_key("S1/2025/00020") == dedupe_key("SI/2025/00020") != dedupe_key("SI/2025/00021")


@pytest.mark.parametrize("read,number", [("S!/2025/00074", "SI/2025/00074"), ("S|/2025/00074", "SI/2025/00074"), ("$1/2025/00074", "S1/2025/00074")])
def test_noise_characters_no_longer_hide_the_number(read, number):
    assert rules.read_invoice_number(TAX_INVOICE.format(read)) == (number, True)


def test_an_amount_is_not_read_as_an_invoice_number():
    assert rules.find_invoice_number("Invoice #: $120.00") is None


def test_ocr_lookalike_is_flagged_but_text_layer_is_trusted():
    text = TAX_INVOICE.format("S1/2025/00074")
    scanned = extractor.extract_invoice(text, ocr_conf=95, mode="rules")
    assert scanned["fields"]["invoice_number"] == "S1/2025/00074"            # reported as read, never silently changed
    assert "lookalike:invoice_number" in scanned["notes"] and scanned["confidence"]["invoice_number"] < 0.6
    digital = extractor.extract_invoice(text, mode="rules")
    assert digital["notes"] == [] and digital["confidence"]["invoice_number"] == 0.9


def test_flagged_number_goes_to_review_with_a_reason():
    inv = {"invoice_number": "S1/2025/7", "invoice_date": "2025-01-10", "supplier_name": "S", "customer_name": "C", "subtotal": 100.0, "tax": 10.0, "total": 110.0, "currency": "USD", "line_items": []}
    issues = check_fields(inv, {"invoice_number": 0.55}, ["lookalike:invoice_number"])
    flagged = [i for i in issues if i["field"] == "invoice_number"]
    assert [i["code"] for i in flagged] == ["uncertain_value"] and "S1/2025/7" in flagged[0]["message"]      # one clear reason, not two
    repaired = [i for i in check_fields(inv, {"invoice_number": 0.75}, ["repaired:invoice_number"]) if i["field"] == "invoice_number"]
    assert [(i["code"], i["severity"]) for i in repaired] == [("lookalike_repaired", "info")]


def _page(n, number):
    return Page(n, TAX_INVOICE.format(number), "ocr", 90.0)


def test_ocr_noise_no_longer_merges_two_invoices():
    segs = segment_pages([_page(1, "20073"), _page(2, "S!/2025/00074"), _page(3, "20075")])
    assert [[p.number for p in s] for s in segs] == [[1], [2], [3]]


def test_unreadable_number_starts_a_new_invoice_instead_of_merging():
    segs = segment_pages([_page(1, "20073"), Page(2, "TAX INVOICE\nInvoice Number: ~~~\nTotal Due 9.00", "ocr", 40.0), _page(3, "20075")])
    assert [[p.number for p in s] for s in segs] == [[1], [2], [3]]


def test_lookalike_reading_of_the_same_number_stays_one_invoice():
    segs = segment_pages([_page(1, "SI/2025/00020"), _page(2, "S1/2025/00020")])
    assert [[p.number for p in s] for s in segs] == [[1, 2]]


def test_pages_without_a_number_field_join_their_invoice():
    cover = Page(1, "Please find our invoice attached", "text")
    first = Page(2, "Invoice No: A-100\nItem A 5.00", "text")
    footer = Page(3, "Item B 7.00\nTotal: 12.00\nPlease quote the invoice number with your payment", "text")
    assert [[p.number for p in s] for s in segment_pages([cover, first, footer])] == [[1, 2, 3]]


def _record(number, supplier="Acme Ltd", flagged=False):
    return SimpleNamespace(invoice_number=number, supplier_name=supplier, dedupe_key=dedupe_key(number), confidence={"invoice_number": 0.55 if flagged else 0.9},
                           extraction_notes=["lookalike:invoice_number"] if flagged else [])


def test_batch_evidence_repairs_lookalike_numbers():
    clean = [_record(f"SI/2025/{k:05d}") for k in range(MIN_CLEAN_READS + 1)]
    misread, noisy = _record("S1/2025/00100", flagged=True), _record("SI/2025/00101", flagged=True)
    reconcile_lookalike_numbers(clean + [misread, noisy])
    assert misread.invoice_number == "SI/2025/00100" and misread.dedupe_key == dedupe_key("SI/2025/00100")
    assert misread.extraction_notes == ["repaired:invoice_number"] and misread.confidence["invoice_number"] > 0.6
    assert noisy.extraction_notes == ["repaired:invoice_number"]               # its reading agreed with the batch, so the flag can go
    assert all(r.extraction_notes == [] for r in clean)


@pytest.mark.parametrize("build", [
    lambda: [_record("SI/2025/1"), _record("SI/2025/2")],                                                     # too little evidence
    lambda: [_record(f"SI/2025/{k}") for k in range(3)] + [_record(f"S1/2025/{k}", flagged=True) for k in range(10, 14)],   # not a majority
    lambda: [_record(f"SI/2025/{k}", supplier="Other Co") for k in range(5)],                                 # another supplier's series
])
def test_weak_evidence_leaves_the_flag_for_a_person(build):
    misread = _record("S1/2025/00100", flagged=True)
    reconcile_lookalike_numbers(build() + [misread])
    assert misread.invoice_number == "S1/2025/00100" and misread.extraction_notes == ["lookalike:invoice_number"]


def test_text_layer_pages_can_confirm_a_real_lookalike_series():
    misread = _record("S1/2025/00100", flagged=True)                           # pages with a text layer never get flagged
    reconcile_lookalike_numbers([_record(f"S1/2025/{k:05d}") for k in range(5)] + [misread])
    assert misread.invoice_number == "S1/2025/00100" and misread.extraction_notes == ["repaired:invoice_number"]


def test_a_damaged_serial_is_not_repaired_even_with_a_good_prefix():
    misread = _record("S1/2025/0091", flagged=True)                            # OCR read 'S1/2025/0091 1'
    reconcile_lookalike_numbers([_record(f"SI/2025/{k:05d}") for k in range(5)] + [misread])
    assert misread.invoice_number == "S1/2025/0091" and misread.extraction_notes == ["lookalike:invoice_number"]


def test_a_number_without_a_fixed_part_is_never_repaired():
    misread = _record("20073", flagged=True)
    reconcile_lookalike_numbers([_record(str(20000 + k)) for k in range(5)] + [misread])
    assert misread.extraction_notes == ["lookalike:invoice_number"]
