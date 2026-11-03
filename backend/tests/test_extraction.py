from app.extraction import extractor
from app.extraction import rules
from app.pipeline import segment_pages
from app.processing.readers import Page

SAMPLE = """Acme Supplies Ltd
INVOICE
Invoice No: INV-77
Invoice Date: 2025-03-14
Due Date: 2025-04-13
Bill To:
Globex Corporation
Description   Qty   Unit Price   Amount
Widget A   2   10.00   20.00
Subtotal:   $20.00
Total:   $20.00"""


def test_rules_extract_core_fields():
    f = rules.extract(SAMPLE)["fields"]
    assert (f["invoice_number"], f["invoice_date"], f["customer_name"], f["total"]) == ("INV-77", "2025-03-14", "Globex Corporation", 20.0)
    assert f["line_items"] == [{"description": "Widget A", "quantity": 2.0, "unit_price": 10.0, "amount": 20.0}]


def test_missing_values_stay_null_not_invented():
    f = rules.extract(SAMPLE)["fields"]
    assert f["tax"] is None                                   # no tax line in the document
    r = rules.extract("Some random letter about nothing\nRegards")
    assert all(r["fields"][k] in (None, []) for k in ("invoice_number", "invoice_date", "subtotal", "tax", "total", "customer_name"))


def test_due_date_is_never_used_as_invoice_date():
    assert rules.extract("Invoice No: A-100\nDue Date: 2025-04-13\nTotal: 5.00")["fields"]["invoice_date"] is None


def test_ambiguous_symbol_currency_has_low_confidence():
    r = rules.extract(SAMPLE)
    assert r["fields"]["currency"] == "USD" and r["confidence"]["currency"] < 0.6


def test_llm_output_is_grounded_and_hallucinations_dropped(monkeypatch):
    fake = {"invoice_number": "INV-77", "invoice_date": "2025-03-14", "supplier_name": "Totally Invented Corp", "customer_name": "Globex Corporation",
            "subtotal": 20, "tax": 1.5, "total": 999.99, "currency": "USD",
            "line_items": [{"description": "Widget A", "quantity": 2, "unit_price": 10, "amount": 20}, {"description": "Ghost item", "quantity": 1, "unit_price": 5, "amount": 5}],
            "confidence": {"supplier_name": 0.99, "total": 0.99, "tax": 0.99}}
    monkeypatch.setattr(extractor, "llm_extract", lambda t: fake)
    r = extractor.extract_invoice(SAMPLE, mode="llm")
    f = r["fields"]
    assert f["total"] == 20.0 or f["total"] is None            # 999.99 is not in the text -> dropped (rules may refill 20.0)
    assert f["supplier_name"] != "Totally Invented Corp"
    assert f["tax"] is None and "ungrounded:tax" in r["notes"]
    assert [i["description"] for i in f["line_items"]] == ["Widget A"]
    assert f["customer_name"] == "Globex Corporation" and r["method"] in ("llm", "llm+rules")


def test_llm_failure_falls_back_to_rules(monkeypatch):
    def boom(_): raise RuntimeError("api down")
    monkeypatch.setattr(extractor, "llm_extract", boom)
    r = extractor.extract_invoice(SAMPLE, mode="llm")
    assert r["method"] == "rules_fallback" and r["fields"]["invoice_number"] == "INV-77" and "llm_failed:RuntimeError" in r["notes"]


def test_low_ocr_confidence_lowers_field_confidence():
    hi = extractor.extract_invoice(SAMPLE, ocr_conf=95, mode="rules")["confidence"]["total"]
    lo = extractor.extract_invoice(SAMPLE, ocr_conf=40, mode="rules")["confidence"]["total"]
    assert lo < hi


def test_segment_pages_splits_on_new_invoice_number():
    mk = lambda n, no: Page(n, f"Invoice No: {no}\nTotal: 5.00" if no else "continuation of previous page", "text")
    segs = segment_pages([mk(1, "A-100"), mk(2, None), mk(3, "A-100"), mk(4, "B-200")])
    assert [[p.number for p in s] for s in segs] == [[1, 2, 3], [4]]
