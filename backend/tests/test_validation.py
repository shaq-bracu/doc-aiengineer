from app.db import SessionLocal, init_db
from app.models import Document, Invoice
from app.validation import apply_validation, check_fields, dedupe_key

OK = {"invoice_number": "A-1", "invoice_date": "2025-01-10", "supplier_name": "S", "customer_name": "C", "subtotal": 100.0, "tax": 10.0,
      "total": 110.0, "currency": "USD", "line_items": [{"description": "x", "quantity": 2, "unit_price": 50.0, "amount": 100.0}]}
codes = lambda inv, **kw: {(i["code"], i["field"]) for i in check_fields(inv, **kw)}


def test_clean_invoice_has_no_issues():
    assert check_fields(OK) == []


def test_missing_required_fields_are_errors():
    inv = {**OK, "invoice_number": None, "total": None}
    errs = {i["field"] for i in check_fields(inv) if i["code"] == "missing_field" and i["severity"] == "error"}
    assert errs == {"invoice_number", "total"}


def test_total_mismatch_detected_and_tolerates_rounding():
    assert ("total_mismatch", "total") in codes({**OK, "total": 150.0})
    assert not any(c == "total_mismatch" for c, _ in codes({**OK, "total": 110.01}))


def test_line_items_vs_subtotal_and_line_math():
    assert ("line_items_mismatch", "line_items") in codes({**OK, "line_items": [{"description": "x", "quantity": 1, "unit_price": 50.0, "amount": 50.0}]})
    assert ("line_item_math", "line_items") in codes({**OK, "line_items": [{"description": "x", "quantity": 3, "unit_price": 50.0, "amount": 100.0}]})


def test_incorrect_numbers_and_dates():
    assert ("invalid_number", "total") in codes({**OK, "total": -5.0})
    assert ("invalid_number", "tax") in codes({**OK}, notes=["unparseable:tax"])
    assert ("invalid_date", "invoice_date") in codes({**OK, "invoice_date": "2999-01-01"})


def test_low_confidence_triggers_warning():
    assert ("low_confidence", "supplier_name") in codes(OK, conf={"supplier_name": 0.3})


def _mk(session, filename, ftype, **kw):
    d = Document(filename=filename, stored_path="/x", file_type=ftype, size_bytes=1, sha256="x")
    session.add(d); session.flush()
    inv = Invoice(document_id=d.id, dedupe_key=dedupe_key(kw.get("invoice_number")), confidence={}, extraction_notes=[], **{**OK, **kw})
    session.add(inv); session.flush()
    return inv


def test_duplicate_and_cross_source_checks():
    init_db()
    with SessionLocal() as s:
        a = _mk(s, "a.pdf", "pdf", invoice_number="DUP-1")
        b = _mk(s, "b.pdf", "pdf", invoice_number="DUP-1")                                        # exact duplicate
        c = _mk(s, "c.xlsx", "xlsx", invoice_number="DUP-1")                                      # same values, other source
        d = _mk(s, "d.xlsx", "xlsx", invoice_number="DUP-1", total=120.0, tax=20.0)               # other source, different total
        e = _mk(s, "e.pdf", "pdf", invoice_number="DUP-1", total=130.0, tax=30.0)                 # same source, different total
        for inv in (a, b, c, d, e):
            apply_validation(s, inv)
        got = lambda inv: {i.code for i in inv.issues}
        assert "duplicate_invoice" not in got(a) and a.review_status == "ok"                       # earliest record wins
        assert "duplicate_invoice" in got(b) and b.review_status == "needs_review"
        assert "cross_verified" in got(c)
        assert "cross_source_mismatch" in got(d)
        assert "duplicate_invoice" in got(e)
        s.rollback()
