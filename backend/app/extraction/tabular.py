"""Deterministic spreadsheet -> invoice parsing (header detection, column aliasing, line-item grouping)."""
import re

import pandas as pd

from ..config import settings
from .utils import normalize_date, parse_amount

ALIASES = {
    "invoice_number": ["invoice number", "invoice no", "invoice no.", "invoice #", "inv no", "inv #", "invoice id", "invoice", "bill no", "inv number"],
    "invoice_date": ["invoice date", "date", "issue date", "date of issue", "billing date", "inv date"],
    "supplier_name": ["supplier", "supplier name", "vendor", "vendor name", "seller", "from"],
    "customer_name": ["customer", "customer name", "client", "bill to", "buyer", "sold to"],
    "subtotal": ["subtotal", "sub total", "net amount", "net total", "amount before tax"],
    "tax": ["tax", "tax amount", "vat", "gst", "sales tax", "vat amount"],
    "total": ["total", "total amount", "grand total", "invoice total", "amount due", "gross amount"],
    "currency": ["currency", "curr", "ccy"],
    "description": ["description", "item", "item description", "product", "particulars", "details"],
    "quantity": ["qty", "quantity", "units"],
    "unit_price": ["unit price", "price", "rate", "unit cost"],
    "line_total": ["line total", "line amount", "item total", "ext price"],
}
_LOOKUP = {a: k for k, v in ALIASES.items() for a in v}


def _hdr(v) -> str:
    return re.sub(r"\s+", " ", str(v).lower().replace("_", " ").replace("-", " ")).strip(" :")


def _map_headers(row) -> dict:
    m = {}
    has_desc = any(_LOOKUP.get(_hdr(v)) == "description" for v in row if pd.notna(v))
    for idx, v in enumerate(row):
        if pd.isna(v):
            continue
        h = _hdr(v)
        key = _LOOKUP.get(h) or (("line_total" if has_desc else "total") if h == "amount" else None)
        if key and key not in m.values():
            m[idx] = key
    return m


def _cell(v):
    return None if (v is None or (not isinstance(v, str) and pd.isna(v)) or str(v).strip() == "") else v


def extract_table(df: pd.DataFrame, sheet: str) -> list[dict]:
    """Return invoice records found in a sheet ([] when the sheet is not a recognisable invoice table)."""
    hdr_i, cols = None, {}
    for i in range(min(25, len(df))):
        m = _map_headers(df.iloc[i].tolist())
        vals = set(m.values())
        if len(m) >= 3 and ("invoice_number" in vals or "total" in vals):
            hdr_i, cols = i, m
            break
    if hdr_i is None:
        return []
    inv_col = next((i for i, k in cols.items() if k == "invoice_number"), None)
    groups: dict[str, dict] = {}
    order, last_key = [], None
    for ridx in range(hdr_i + 1, len(df)):
        row = {k: _cell(df.iat[ridx, i]) for i, k in cols.items()}
        if not any(v is not None for v in row.values()):
            continue
        key = str(row["invoice_number"]).strip() if row.get("invoice_number") is not None else last_key
        if key is None:
            continue
        last_key = key
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"rows": [], "first_row": ridx + 1}
            order.append(key)
        g["rows"].append(row)
    out = []
    for key in order:
        rows = groups[key]["rows"]

        def first(field):
            return next((r[field] for r in rows if r.get(field) is not None), None)

        date_raw = first("invoice_date")
        iso, dconf = normalize_date(date_raw, settings.DATE_DAYFIRST)
        notes = []
        if date_raw is not None and iso is None:
            notes.append("unparseable:invoice_date")
        f = {"invoice_number": key, "invoice_date": iso,
             "supplier_name": (str(first("supplier_name")).strip() if first("supplier_name") is not None else None),
             "customer_name": (str(first("customer_name")).strip() if first("customer_name") is not None else None)}
        for k in ("subtotal", "tax", "total"):
            raw = first(k)
            f[k] = parse_amount(raw)
            if raw is not None and f[k] is None:
                notes.append(f"unparseable:{k}")
        cur = first("currency")
        f["currency"] = str(cur).strip().upper()[:3] if cur is not None else None
        items = []
        for r in rows:
            if r.get("description") is not None:
                items.append({"description": str(r["description"]).strip(), "quantity": parse_amount(r.get("quantity")),
                              "unit_price": parse_amount(r.get("unit_price")), "amount": parse_amount(r.get("line_total"))})
        f["line_items"] = items
        conf = {k: (0.95 if f.get(k) not in (None, []) else 0.0) for k in f}
        if iso:
            conf["invoice_date"] = min(0.95, dconf)
        out.append({"fields": f, "confidence": conf, "notes": notes, "method": "spreadsheet",
                    "source_ref": f"{sheet}!row {groups[key]['first_row']}",
                    "raw_text": "\n".join(" | ".join(str(v) for v in r.values() if v is not None) for r in rows)})
    return out
