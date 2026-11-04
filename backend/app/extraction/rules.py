"""Deterministic, label-driven extractor. Fast, free, offline; also the fallback and the cross-check for the LLM."""
import re
from collections import Counter

from ..config import settings
from .utils import CURRENCY_SYMBOLS, ISO_CODES, find_dates, normalize_date, parse_amount

_STOP = (r"(?:invoice|inv|date|due|total|tax|vat|po|p\.o|phone|tel|email|e-mail|address|currency|bill|billed|"
         r"ship|terms|ref|issued|customer|supplier|vendor)")
_STOP_RX = re.compile(rf"\s+(?={_STOP}\b[^:,]{{0,30}}:)", re.I)
_LABEL_LINE = re.compile(r"^\s*[A-Za-z #.]{2,25}:\s*\S")

# ! | $ are never part of an invoice number, but OCR reads a bare I as ! or | and an S as $
INV_NO = re.compile(r"(?:invoice|inv)\s*(?:(?:number|num|no|id)\b\.?|#)\s*[:#.\-]?\s*([A-Z0-9$][A-Z0-9\-/_.!|$]{1,30})", re.I)
INV_NO2 = re.compile(r"\binvoice\s*[:#]\s*([A-Z0-9$][A-Z0-9\-/_.!|$]{2,30})", re.I)
# a header field ('Invoice Number:', 'Invoice #'), not a phrase like 'please quote the invoice number when paying'
INV_LABEL = re.compile(r"\b(?:invoice|inv)\s*(?:(?:number|num|no|id)\b\.?\s*[:#]|#)", re.I)

SUB_RX = re.compile(r"^\W*(?:sub[\s\-]?total|net\s+(?:amount|total)|total\s+before\s+tax|amount\s+before\s+tax|taxable\s+(?:amount|value))\b", re.I)
TAX_RX = re.compile(r"^\W*(?:total\s+|sales\s+)?(?:tax|vat|gst|hst)\b(?!\s*(?:id|no\b|number|reg|#))", re.I)
TOTAL_RX = [re.compile(r"^\W*(?:grand\s+total|total\s+due|amount\s+due|balance\s+due|total\s+amount|invoice\s+total|total\s+payable|amount\s+payable)\b", re.I),
            re.compile(r"^\W*total\b", re.I)]
SUMMARY_RX = re.compile(r"^\W*(?:sub[\s\-]?total|net\s+(?:amount|total)|total|grand\s+total|tax|vat|gst|balance|amount\s+due|payment|notes?)\b", re.I)

ROW_FULL = re.compile(r"^(?P<d>.*?[A-Za-z].*?)\s+(?P<q>\d+(?:\.\d+)?)\s+(?P<p>\d[\d,]*\.\d{1,2})\s+(?P<a>\d[\d,]*\.\d{1,2})$")
ROW_2 = re.compile(r"^(?P<d>.*?[A-Za-z].*?)\s+(?P<a>\d[\d,]*\.\d{1,2})$")
ROW_TAIL = re.compile(r"^(?P<d>.*?[A-Za-z].*?)\s+(?P<q>\S{1,3})\s+(?P<p>\d[\d,]*[.,]\d{1,2})$")


def _recover_row(desc: str, amount):
    """OCR noise in the quantity ('2.', '5:', 'T') breaks the full row pattern and leaves quantity and price glued to the
    description. The row's own arithmetic shows where the columns are; a quantity is kept only if the page shows it."""
    m = ROW_TAIL.match(desc)
    price = parse_amount(m["p"]) if m else None
    if not price or amount is None:
        return None
    units = amount / price
    if abs(units - round(units)) > 1e-6 or not 1 <= round(units) <= 99999:
        return None
    shown = re.sub(r"[^\d]", "", m["q"])
    if not (len(m["q"]) == 1 or (shown and int(shown) == round(units))):
        return None                                   # e.g. 'Bolts M8 2.50': M8 belongs to the description
    return {"description": m["d"].strip(), "quantity": float(round(units)) if shown and int(shown) == round(units) else None,
            "unit_price": price, "amount": amount}


def clean_value(s: str) -> str:
    s = re.split(r"\s{3,}|\t|\|", s)[0]   # a 3+ space gap = next table column (empty value => "")
    s = _STOP_RX.split(s)[0]
    s = re.sub(r"\b(?:tax\s+)?invoice\s*$", "", s, flags=re.I)
    return s.strip(" :-|,;")


def label_value(lines, pattern, lookahead=2):
    rx_start = re.compile(rf"^\W*(?:{pattern})\b\s{{0,2}}[:#\-]?(.*)$", re.I)
    rx_colon = re.compile(rf"\b(?:{pattern})\b\s*:(.*)$", re.I)
    for i, line in enumerate(lines):
        m = rx_start.search(line) or rx_colon.search(line)
        if not m:
            continue
        v = clean_value(m.group(1))
        if v:
            return v, i
        for j in range(i + 1, min(len(lines), i + 1 + lookahead)):
            if _LABEL_LINE.match(lines[j]):
                break
            nxt = clean_value(lines[j])
            if nxt:
                return nxt, j
    return None, None


def _undo_ocr_noise(v: str) -> str:
    out = []
    for i, ch in enumerate(v):
        if ch in "!|":
            ch = "I" if i and v[i - 1].isalpha() else "1"
        elif ch == "$":
            ch = "S"
        out.append(ch)
    return "".join(out)


def read_invoice_number(text: str):
    """-> (number | None, noisy): `noisy` means OCR-noise characters had to be mapped back to get the number."""
    for rx in (INV_NO, INV_NO2):
        for m in rx.finditer(text):
            raw = m.group(1).strip(".-/_|!")
            if re.fullmatch(r"\$[\d.,]+", raw):       # "Invoice #: $120.00" is an amount, not a number
                continue
            v = _undo_ocr_noise(raw)
            if re.search(r"\d", v) and len(v) >= 3:
                return v, v != raw
    return None, False


def find_invoice_number(text: str):
    return read_invoice_number(text)[0]


def has_invoice_label(text: str) -> bool:
    return bool(INV_LABEL.search(text))


def amount_on_line(line: str):
    s = re.sub(r"\(?\d+(?:\.\d+)?\s*%\)?", " ", line)
    for n in reversed(re.findall(r"\d[\d,]*(?:\.\d+)?", s)):
        v = parse_amount(n)
        if v is not None:
            return v
    return None


def detect_currency(text: str):
    m = re.search(r"currency\s*[:\-]?\s*([A-Z]{3})\b", text, re.I)
    if m and m.group(1).upper() in ISO_CODES:
        return m.group(1).upper(), 0.95
    codes = Counter(c for c in re.findall(r"\b[A-Z]{3}\b", text) if c in ISO_CODES)
    if codes:
        return codes.most_common(1)[0][0], 0.9
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code, 0.55 if sym in "$¥" else 0.85   # '$' / '¥' are ambiguous across currencies
    if re.search(r"\bTk\.?\s*\d", text):
        return "BDT", 0.8
    return None, 0.0


def extract_line_items(lines):
    hdr = None
    for i, l in enumerate(lines):
        if re.search(r"\b(description|item|particulars|product|details)\b", l, re.I) and \
                re.search(r"\b(qty|quantity|unit price|price|rate|amount|total)\b", l, re.I) and not amount_on_line(l):
            hdr = i
            break
    if hdr is None:
        return []
    items = []
    for l in lines[hdr + 1:]:
        if SUMMARY_RX.match(l):
            break
        s = re.sub(r"[$€£৳₹¥]", "", l).strip()
        m = ROW_FULL.match(s)
        if m:
            d = re.sub(r"^\d{1,3}[.)]?\s+(?=[A-Za-z])", "", m["d"]).strip()
            items.append({"description": d, "quantity": float(m["q"]), "unit_price": parse_amount(m["p"]), "amount": parse_amount(m["a"])})
            continue
        m = ROW_2.match(s)
        if m:
            d = re.sub(r"^\d{1,3}[.)]?\s+(?=[A-Za-z])", "", m["d"]).strip()
            amount = parse_amount(m["a"])
            items.append(_recover_row(d, amount) or {"description": d, "quantity": None, "unit_price": None, "amount": amount})
    return items


def extract(text: str) -> dict:
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    f, c = {}, {}

    f["invoice_number"], noisy_number = read_invoice_number(text)
    c["invoice_number"] = 0.9 if f["invoice_number"] else 0.0

    # date: prefer an explicitly labelled invoice date, never a due date
    f["invoice_date"], c["invoice_date"] = None, 0.0
    for pat in (r"invoice\s*date", r"date\s+of\s+issue|issue\s+date|issued(?:\s+on)?", r"(?<!due )(?<!due-)date"):
        v, _ = label_value(lines, pat, lookahead=0)
        toks = find_dates(v) if v else []
        if toks:
            iso, conf = normalize_date(toks[0], settings.DATE_DAYFIRST)
            if iso:
                f["invoice_date"], c["invoice_date"] = iso, min(conf, 0.9 if conf > 0.6 else conf)
                break

    f["supplier_name"], c["supplier_name"] = None, 0.0
    v, _ = label_value(lines, r"supplier|vendor|seller|sold by|issued by|billed from|from")
    if v:
        f["supplier_name"], c["supplier_name"] = v, 0.85
    else:  # heuristic: first plain line near the top that is not a label/title
        for l in lines[:8]:
            cand = clean_value(l)
            if len(re.findall(r"[A-Za-z]", cand)) >= 3 and ":" not in cand and \
                    not re.search(r"\b(invoice|bill|date|page|receipt)\b", cand, re.I):
                f["supplier_name"], c["supplier_name"] = cand, (0.65 if l is lines[0] else 0.5)
                break

    v, _ = label_value(lines, r"bill(?:ed)?\s+to|customer(?:\s+name)?|client|sold\s+to|buyer")
    f["customer_name"], c["customer_name"] = (v, 0.85) if v else (None, 0.0)

    f["subtotal"] = f["tax"] = f["total"] = None
    totals = {0: None, 1: None}
    for l in lines:
        if SUB_RX.match(l) and f["subtotal"] is None:
            f["subtotal"] = amount_on_line(l)
        elif TAX_RX.match(l) and not re.search(r"invoice", l, re.I) and f["tax"] is None:
            f["tax"] = amount_on_line(l)
        else:
            for k, rx in enumerate(TOTAL_RX):
                if rx.match(l) and not SUB_RX.match(l) and not TAX_RX.match(l):
                    a = amount_on_line(l)
                    if a is not None:
                        totals[k] = a
                    break
    f["total"] = totals[0] if totals[0] is not None else totals[1]
    for k in ("subtotal", "tax", "total"):
        c[k] = 0.9 if f[k] is not None else 0.0

    f["currency"], c["currency"] = detect_currency(text)
    f["line_items"] = extract_line_items(lines)
    c["line_items"] = 0.85 if f["line_items"] else 0.0
    return {"fields": f, "confidence": c, "noisy_number": noisy_number}
