"""Stress test: one scanned PDF of about 1000 pages through a running API, scored page by page against ground truth.

    python -m evaluation.run_scale --api http://localhost:8000       # generates the scan if needed, uploads, waits, scores
    python -m evaluation.run_scale --api URL --score-only            # scores a copy the API has already processed

Writes evaluation/results/scale.json; run_eval.py adds it to the evaluation report.
"""
import argparse
import json
import os
import time
from pathlib import Path

import httpx
from rapidfuzz import fuzz

from evaluation.synth import build_scan

NAME = "scan_1000_pages.pdf"
SCALARS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]


def score(doc: dict, truth: list[dict]) -> dict:
    """Each record is compared with the invoice labelled on its first page, so one misread field never fails the others."""
    right = dict.fromkeys(SCALARS, 0)
    all_right = wrong_numbers = wrong_flagged = repaired = repaired_right = rows_right = rows_total = review_fields = review_rows = 0
    for inv in doc["invoices"]:
        t = truth[inv["page_start"] - 1]
        ok = True
        for f in SCALARS:
            if inv[f] == t[f]:
                right[f] += 1
            else:
                ok = False
        all_right += ok
        if inv["review_status"] == "needs_review":
            review_fields += not ok
            review_rows += ok and any(i["code"] == "line_items_mismatch" for i in inv["issues"])
        if inv["invoice_number"] != t["invoice_number"]:
            wrong_numbers += 1
            wrong_flagged += inv["review_status"] == "needs_review"
        if any(i["code"] == "lookalike_repaired" for i in inv["issues"]):
            repaired += 1
            repaired_right += inv["invoice_number"] == t["invoice_number"]
        rows_total += len(t["line_items"])
        for row in t["line_items"]:
            rows_right += any(p["amount"] == row["amount"] and fuzz.ratio(p["description"] or "", row["description"]) >= 90 for p in inv["line_items"])
    n = len(doc["invoices"])
    return {
        "pages": doc["page_count"], "ocr_pages": doc["ocr_pages"], "mean_ocr_conf": round(doc["mean_ocr_conf"], 1), "size_mb": round(doc["size_bytes"] / 1e6, 1),
        "status": doc["status"], "error": doc["error"], "processing_s": round(doc["duration_ms"] / 1000), "s_per_page": round(doc["duration_ms"] / 1000 / doc["page_count"], 3),
        "cpu_cores": os.cpu_count(), "labelled": len(truth), "invoices_found": n, "merged_records": sum(i["page_end"] > i["page_start"] for i in doc["invoices"]),
        "field_accuracy": {f: round(right[f] / n, 4) for f in SCALARS}, "line_item_rows_right": round(rows_right / max(1, rows_total), 4),
        "invoices_all_fields_right": all_right, "wrong_invoice_numbers": wrong_numbers, "wrong_numbers_flagged": wrong_flagged,
        "numbers_repaired": repaired, "repairs_correct": repaired_right, "needs_review": sum(i["review_status"] == "needs_review" for i in doc["invoices"]),
        "review_with_wrong_field": review_fields, "review_for_line_items_only": review_rows,
    }


def scale_lines(r: dict) -> list[str]:
    """The report section for a scale.json result."""
    mins, secs = divmod(r["processing_s"], 60)
    lines = ["## 1,000-page scan", "",
             f"One synthetic scanned PDF (150 dpi, JPEG quality 50, {r['size_mb']} MB, one invoice per page) through the running Docker stack, scored per record found.", "",
             "| Metric | Value |", "|---|---|",
             f"| Processing time | **{mins} min {secs} s** ({r['s_per_page']} s per page, default 4 OCR threads, {r['cpu_cores']} CPU cores) |",
             f"| Invoices found | **{r['invoices_found']} of {r['labelled']}** ({r['merged_records']} records span two pages) |",
             f"| Invoices with every scalar field right | {r['invoices_all_fields_right']} |",
             f"| Line item rows read correctly | {100 * r['line_item_rows_right']:.1f}% |",
             f"| Invoice numbers still wrong | {r['wrong_invoice_numbers']}, of which {r['wrong_numbers_flagged']} are flagged for review |",
             f"| Numbers repaired from same-supplier evidence | {r['numbers_repaired']}, of which {r['repairs_correct']} are correct |",
             f"| Invoices sent to review | {r['needs_review']}: {r['review_with_wrong_field']} with a wrong field, {r['review_for_line_items_only']} whose line items do not add up |", "",
             "| Field | Accuracy |", "|---|---|"]
    return lines + [f"| {k} | {100 * v:.1f}% |" for k, v in r["field_accuracy"].items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--data", default="evaluation/data", help="where the scan and its labels live")
    ap.add_argument("--out", default="evaluation/results")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()
    data, out = Path(args.data), Path(args.out)
    data.mkdir(parents=True, exist_ok=True)
    pdf, labels = data / NAME, data / (NAME.removesuffix(".pdf") + ".labels.json")
    if not labels.exists():
        print("generating the scan...")
        labels.write_text(json.dumps(build_scan(pdf, 1000)))
    truth = json.loads(labels.read_text())
    client = httpx.Client(base_url=args.api, timeout=300)

    def find():
        return next((d for d in client.get("/api/documents", params={"limit": 200}).json()["items"] if d["filename"] == NAME), None)

    if args.score_only:
        doc_id = find()["id"]
    else:
        with pdf.open("rb") as f:
            r = client.post("/api/documents", files={"files": (NAME, f, "application/pdf")})
        r.raise_for_status()
        doc_id = r.json()["results"][0]["document"]["id"]
        print("uploaded as document", doc_id)
    while (cur := find())["id"] != doc_id or cur["status"] in ("queued", "processing"):
        time.sleep(3)
    t0 = time.perf_counter()
    doc = client.get(f"/api/documents/{doc_id}").json()
    result = {**score(doc, truth), "detail_api_ms": round((time.perf_counter() - t0) * 1000)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "scale.json").write_text(json.dumps(result, indent=1))
    print("\n".join(scale_lines(result)))


if __name__ == "__main__":
    main()
