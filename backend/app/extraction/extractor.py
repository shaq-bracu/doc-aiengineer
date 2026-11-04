"""Orchestrates rules + LLM, normalises types, and GROUNDS every value in the source text (no invention)."""
import logging
import re

from ..config import settings
from . import rules
from .llm import llm_available, llm_extract
from .utils import (CURRENCY_SYMBOLS, ISO_CODES, amount_in_text, clip_text, date_in_text, has_lookalike, id_in_text,
                    norm_text, normalize_date, parse_amount, text_contains)

log = logging.getLogger("extract")
SCALARS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
FIELDS = SCALARS + ["line_items"]
LOOKALIKE_CONFIDENCE = 0.55   # below the review threshold


def _str(v):
    v = None if v is None else str(v).strip()
    return v or None


def _ground(f: dict, c: dict, text: str, notes: list):
    """Drop (set to null) any value that cannot be found in the document text."""
    tn = norm_text(text)
    for k in SCALARS:
        v = f.get(k)
        if v is None:
            continue
        if k == "invoice_number":
            ok = id_in_text(text, v)
        elif k in ("supplier_name", "customer_name"):
            ok = text_contains(tn, v)
        elif k == "invoice_date":
            ok = date_in_text(text, v)
        elif k == "currency":
            ok = bool(re.search(rf"\b{v}\b", text, re.I)) or any(s in text for s, code in CURRENCY_SYMBOLS.items() if code == v)
        else:
            ok = amount_in_text(text, v)
        if not ok:
            notes.append(f"ungrounded:{k}")
            f[k], c[k] = None, 0.0
    kept = []
    for it in f.get("line_items") or []:
        d, a = it.get("description"), it.get("amount")
        if (d and not text_contains(tn, d, 80)) or (a is not None and not amount_in_text(text, a)):
            notes.append("ungrounded:line_item")
            continue
        kept.append(it)
    f["line_items"] = kept


def _normalize(raw: dict, notes: list) -> tuple[dict, dict]:
    f, c = {}, {}
    rc = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
    for k in ("invoice_number", "supplier_name", "customer_name"):
        f[k] = _str(raw.get(k))
    iso, dconf = normalize_date(raw.get("invoice_date"), settings.DATE_DAYFIRST)
    if raw.get("invoice_date") not in (None, "") and iso is None:
        notes.append("unparseable:invoice_date")
    f["invoice_date"] = iso
    for k in ("subtotal", "tax", "total"):
        f[k] = parse_amount(raw.get(k))
        if raw.get(k) not in (None, "") and f[k] is None:
            notes.append(f"unparseable:{k}")
    cur = _str(raw.get("currency"))
    cur = CURRENCY_SYMBOLS.get(cur, cur.upper() if cur else None)
    f["currency"] = cur if cur in ISO_CODES else None
    items = []
    for it in raw.get("line_items") or []:
        if isinstance(it, dict):
            items.append({"description": _str(it.get("description")), "quantity": parse_amount(it.get("quantity")),
                          "unit_price": parse_amount(it.get("unit_price")), "amount": parse_amount(it.get("amount"))})
    f["line_items"] = items
    for k in FIELDS:
        try:
            c[k] = max(0.0, min(1.0, float(rc.get(k, 0.8))))
        except (TypeError, ValueError):
            c[k] = 0.8
    if f["invoice_date"]:
        c["invoice_date"] = min(c["invoice_date"], dconf if dconf else 1.0)
    return f, c


def extract_invoice(text: str, ocr_conf: float | None = None, mode: str | None = None) -> dict:
    """-> {fields, confidence, notes, method}. Missing/uncertain values stay null."""
    mode = mode or settings.EXTRACTION_MODE
    text = clip_text(text)
    notes: list[str] = []
    r = rules.extract(text)
    use_llm = mode == "llm" or (mode == "auto" and llm_available())
    method = "rules"
    f, c = r["fields"], dict(r["confidence"])
    if use_llm:
        try:
            lf, lc = _normalize(llm_extract(text), notes)
            _ground(lf, lc, text, notes)
            f, c, method = lf, lc, "llm"
            for k in FIELDS:  # fill gaps with confident rule hits
                if (f.get(k) in (None, [])) and r["fields"].get(k) not in (None, []) and r["confidence"][k] >= 0.7:
                    f[k], c[k], method = r["fields"][k], r["confidence"][k], "llm+rules"
        except Exception as e:  # network/API/schema failure must never lose the document
            log.warning("LLM extraction failed, using rules: %s", e)
            notes.append(f"llm_failed:{type(e).__name__}")
            method = "rules_fallback"
    if ocr_conf is not None:  # OCR noise lowers trust in every field
        scale = max(0.5, min(1.0, ocr_conf / 80.0))
        c = {k: round(v * scale, 3) for k, v in c.items()}
        number = f.get("invoice_number")
        if number and (has_lookalike(number) or (r["noisy_number"] and number == r["fields"]["invoice_number"])):
            notes.append("lookalike:invoice_number")
            c["invoice_number"] = min(c["invoice_number"], LOOKALIKE_CONFIDENCE)
    return {"fields": f, "confidence": c, "notes": notes, "method": method}
