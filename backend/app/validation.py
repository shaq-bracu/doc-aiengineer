"""Business-rule validation. Pure checks + DB-aware duplicate / cross-source checks."""
import re
from datetime import date, timedelta

from sqlalchemy import select

from .config import settings
from .extraction.utils import compact, fold_lookalikes, norm_text
from .models import Invoice, ValidationIssue

REQUIRED_ERROR = ["invoice_number", "invoice_date", "supplier_name", "total"]
REQUIRED_WARN = ["customer_name", "currency", "subtotal", "tax"]
CONFIDENCE_CHECKED = REQUIRED_ERROR + ["customer_name"]
SHEET_TYPES = {"xlsx", "xls", "csv"}


def _issue(code, severity, field, message):
    return {"code": code, "severity": severity, "field": field, "message": message}


def dedupe_key(invoice_number):
    """Look-alike characters fold together, so a scan's S1/2025/7 still meets the spreadsheet's SI/2025/7."""
    k = fold_lookalikes(compact(invoice_number)) if invoice_number else ""
    return k[:80] or None


def check_fields(inv: dict, conf: dict | None = None, notes: list | None = None) -> list[dict]:
    """Rules that need only the record itself. `inv` is a plain dict of the invoice fields."""
    out, tol = [], settings.AMOUNT_TOLERANCE
    for k in REQUIRED_ERROR:
        if inv.get(k) in (None, ""):
            out.append(_issue("missing_field", "error", k, f"Required field '{k}' is missing"))
    for k in REQUIRED_WARN:
        if inv.get(k) in (None, ""):
            out.append(_issue("missing_field", "warning", k, f"Field '{k}' is missing"))
    if not inv.get("line_items"):
        out.append(_issue("missing_field", "warning", "line_items", "No line items were found"))
    for n in notes or []:
        kind, _, what = n.partition(":")
        if kind == "unparseable":
            out.append(_issue("invalid_number" if what in ("subtotal", "tax", "total") else "invalid_date", "error", what,
                              f"Value for '{what}' could not be parsed"))
        elif kind == "ungrounded":
            out.append(_issue("uncertain_value", "warning", what, f"A value for '{what}' was not found in the source text and was dropped"))
        elif kind == "lookalike":
            out.append(_issue("uncertain_value", "warning", what, f"OCR read this as '{inv.get(what)}', which contains characters it often confuses, such as I and 1. Check it against the source."))
        elif kind == "repaired":
            out.append(_issue("lookalike_repaired", "info", what, "OCR read a look-alike character here. Corrected to match this supplier's other invoices in the file."))

    sub, tax, tot = inv.get("subtotal"), inv.get("tax"), inv.get("total")
    for k, v in (("subtotal", sub), ("tax", tax), ("total", tot)):
        if v is None:
            continue
        if v < 0:
            out.append(_issue("invalid_number", "error", k, f"'{k}' is negative ({v})"))
        elif v > 1e9:
            out.append(_issue("invalid_number", "error", k, f"'{k}' is implausibly large ({v})"))
    if tot is not None and tot == 0:
        out.append(_issue("invalid_number", "error", "total", "Total is zero"))
    if tax is not None and sub is not None and tax > sub > 0:
        out.append(_issue("tax_exceeds_subtotal", "warning", "tax", f"Tax ({tax}) is larger than subtotal ({sub})"))

    d = inv.get("invoice_date")
    if d:
        y, m, dd = map(int, d.split("-"))
        if date(y, m, dd) > date.today() + timedelta(days=1):
            out.append(_issue("invalid_date", "warning", "invoice_date", f"Invoice date {d} is in the future"))
        elif y < 1990:
            out.append(_issue("invalid_date", "warning", "invoice_date", f"Invoice date {d} is implausibly old"))

    items = inv.get("line_items") or []
    amounts = [i["amount"] for i in items if i.get("amount") is not None]
    line_sum = round(sum(amounts), 2) if amounts else None
    for n, it in enumerate(items, 1):
        q, p, a = it.get("quantity"), it.get("unit_price"), it.get("amount")
        if None not in (q, p, a) and abs(q * p - a) > tol + 0.005 * abs(a):
            out.append(_issue("line_item_math", "warning", "line_items", f"Line {n}: {q} x {p} = {round(q * p, 2)}, but amount is {a}"))
    if sub is not None and tax is not None and tot is not None:
        if abs(sub + tax - tot) > tol:
            out.append(_issue("total_mismatch", "error", "total", f"Subtotal {sub} + tax {tax} = {round(sub + tax, 2)}, but total is {tot}"))
    elif sub is not None and tot is not None and tax is None and abs(sub - tot) > tol:
        out.append(_issue("total_mismatch", "warning", "total", f"Total ({tot}) differs from subtotal ({sub}) and no tax was found"))
    if line_sum is not None:
        tol_l = tol + 0.01 * len(amounts)
        ref, ref_name = (sub, "subtotal") if sub is not None else ((round(tot - tax, 2), "total - tax") if (tot is not None and tax is not None) else (None, None))
        if ref is not None and abs(line_sum - ref) > tol_l:
            out.append(_issue("line_items_mismatch", "error", "line_items", f"Line items sum to {line_sum}, but {ref_name} is {ref}"))

    th = settings.REVIEW_CONFIDENCE_THRESHOLD
    explained = {i["field"] for i in out if i["code"] == "uncertain_value"}
    for k in CONFIDENCE_CHECKED:
        if k not in explained and inv.get(k) not in (None, "") and (conf or {}).get(k, 1.0) < th:
            out.append(_issue("low_confidence", "warning", k, f"Low confidence ({(conf or {}).get(k):.2f}) for '{k}'"))
    return out


def _supplier_compatible(a, b):
    return not a or not b or norm_text(a) == norm_text(b)


def check_duplicates(session, inv: Invoice) -> list[dict]:
    """Duplicate invoices and PDF<->Excel (or PDF<->PDF) cross-verification. Earlier record wins."""
    if not inv.dedupe_key:
        return []
    out, tol = [], settings.AMOUNT_TOLERANCE
    others = session.scalars(select(Invoice).where(Invoice.dedupe_key == inv.dedupe_key, Invoice.id < inv.id)).all()
    for o in others:
        if not _supplier_compatible(o.supplier_name, inv.supplier_name):
            continue
        diffs = []
        if None not in (o.total, inv.total) and abs(o.total - inv.total) > tol:
            diffs.append(f"total {o.total} vs {inv.total}")
        if o.invoice_date and inv.invoice_date and o.invoice_date != inv.invoice_date:
            diffs.append(f"date {o.invoice_date} vs {inv.invoice_date}")
        if o.customer_name and inv.customer_name and norm_text(o.customer_name) != norm_text(inv.customer_name):
            diffs.append("customer differs")
        cross = (o.document.file_type in SHEET_TYPES) != (inv.document.file_type in SHEET_TYPES)
        where = f"invoice #{o.id} ({o.document.filename})"
        if diffs and cross:
            out.append(_issue("cross_source_mismatch", "error", None, f"Same invoice number as {where} but values differ: " + "; ".join(diffs)))
        elif diffs:
            out.append(_issue("duplicate_invoice", "error", "invoice_number", f"Same invoice number as {where} with different values: " + "; ".join(diffs)))
        elif cross:
            out.append(_issue("cross_verified", "info", None, f"Matches {where} from another source (PDF/image vs spreadsheet)"))
        else:
            out.append(_issue("duplicate_invoice", "error", "invoice_number", f"Exact duplicate of {where}"))
    return out


def refresh_dedupe_keys(session) -> int:
    """Bring rows stored before the key format changed up to date, so old invoices still meet new ones."""
    changed = 0
    for inv in session.scalars(select(Invoice).where(Invoice.invoice_number.is_not(None))):
        key = dedupe_key(inv.invoice_number)
        if inv.dedupe_key != key:
            inv.dedupe_key = key
            changed += 1
    return changed


def apply_validation(session, inv: Invoice) -> None:
    """(Re)run all checks, replace unresolved issues, and set review_status."""
    for old in list(inv.issues):
        if not old.resolved:
            session.delete(old)
    session.flush()
    session.expire(inv, ["issues"])
    rec = {k: getattr(inv, k) for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency", "line_items")}
    found = check_fields(rec, inv.confidence, inv.extraction_notes) + check_duplicates(session, inv)
    for i in found:
        session.add(ValidationIssue(document_id=inv.document_id, invoice_id=inv.id, **i))
    needs = any(i["severity"] == "error" or i["code"] in ("low_confidence", "uncertain_value") for i in found)
    inv.review_status = "needs_review" if needs else "ok"
    session.flush()
    session.expire(inv, ["issues"])
