"""Parsing + grounding helpers shared by every extractor."""
import math
import re
from datetime import date, datetime

from dateutil import parser as dparser
from rapidfuzz import fuzz

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "৳": "BDT", "₹": "INR", "¥": "JPY"}
ISO_CODES = {"USD", "EUR", "GBP", "BDT", "INR", "JPY", "CAD", "AUD", "CHF", "CNY", "SGD", "AED", "SAR", "MYR", "NZD", "HKD", "SEK", "NOK", "DKK", "ZAR"}


def parse_amount(v):
    """'1,234.50' | '1.234,50' | '(120.00)' | 120 -> float, or None. Never guesses on garbage."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "null", "-"):
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-")
    s = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + ("." + tail if len(tail) != 3 else tail)
    try:
        val = float(s)
    except ValueError:
        return None
    return round(-val if neg else val, 2)


_DATE_TOKENS = [
    re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"),
    re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?[\s-]+[A-Za-z]{3,9}\.?,?[\s-]+\d{4}\b"),
    re.compile(r"\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"),
]


def find_dates(s: str) -> list[str]:
    hits = []
    for rx in _DATE_TOKENS:
        hits += [(m.start(), m.group(0)) for m in rx.finditer(s)]
    return [t for _, t in sorted(hits)]


def normalize_date(v, dayfirst: bool = True):
    """-> (iso 'YYYY-MM-DD' | None, confidence). Ambiguous dd/mm vs mm/dd gets low confidence."""
    if v is None:
        return None, 0.0
    if isinstance(v, datetime):
        return v.date().isoformat(), 1.0
    if isinstance(v, date):
        return v.isoformat(), 1.0
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none", "null"):
        return None, 0.0
    try:
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?", s)
        if m:
            y, mo, d = map(int, m.groups())
            return date(y, mo, d).isoformat(), 1.0
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
        if m:
            a, b, y = map(int, m.groups())
            y = y + 2000 if y < 100 else y
            if a > 12 and b <= 12:
                return date(y, b, a).isoformat(), 0.95
            if b > 12 and a <= 12:
                return date(y, a, b).isoformat(), 0.95
            if a == b:
                return date(y, a, b).isoformat(), 0.95
            d, mo = (a, b) if dayfirst else (b, a)
            return date(y, mo, d).isoformat(), 0.6   # ambiguous -> flagged for review
        dt = dparser.parse(s, dayfirst=dayfirst, default=datetime(1900, 1, 1))
        if dt.year == 1900:
            return None, 0.0
        return dt.date().isoformat(), 0.9
    except (ValueError, OverflowError):
        return None, 0.0


def norm_text(s: str) -> str:
    return re.sub(r"[\W_]+", " ", str(s).casefold(), flags=re.UNICODE).strip()


def compact(s: str) -> str:
    return re.sub(r"[\W_]+", "", str(s).casefold(), flags=re.UNICODE)


def text_contains(text_norm: str, value: str, threshold: int = 85) -> bool:
    v = norm_text(value)
    if not v:
        return False
    if v in text_norm:
        return True
    return len(v) >= 5 and fuzz.partial_ratio(v, text_norm) >= threshold


def id_in_text(text: str, value: str) -> bool:
    v, t = compact(value), compact(text)
    if not v:
        return False
    return v in t or (len(v) >= 6 and fuzz.partial_ratio(v, t) >= 90)


def amount_in_text(text: str, value) -> bool:
    if value is None:
        return False
    cands = {f"{value:.2f}", f"{value:,.2f}", f"{value:.2f}".replace(".", ","),
             f"{value:,.2f}".replace(",", " ").replace(".", ",")}
    if float(value).is_integer():
        cands |= {str(int(value)), f"{int(value):,}"}
    if round(value * 10) == value * 10:
        cands.add(f"{value:.1f}")
    return any(re.search(rf"(?<![\d.,]){re.escape(c)}(?!\d)", text) for c in cands)


def date_in_text(text: str, iso: str) -> bool:
    for tok in find_dates(text):
        for df in (True, False):
            if normalize_date(tok, df)[0] == iso:
                return True
    return False


def clip_text(text: str, head: int = 14000, tail: int = 10000) -> str:
    return text if len(text) <= head + tail else text[:head] + "\n[...]\n" + text[-tail:]
