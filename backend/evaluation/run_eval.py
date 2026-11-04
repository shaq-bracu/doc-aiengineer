"""Measure extraction accuracy, speed, success/failure rate, validation recall and search hit-rate on a labelled dataset.

    python -m evaluation.run_eval                      # generates the synthetic dataset, runs, writes the report
    python -m evaluation.run_eval --data DIR           # your own data: DIR/labels.json in the same format (see synth.py)
"""
import argparse
import json
import os
import platform
import statistics as st
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--data", default=None)
ap.add_argument("--out", default="evaluation/results")
ap.add_argument("--seed", type=int, default=7)
args = ap.parse_args()

tmp = Path(tempfile.mkdtemp(prefix="docintel_eval_"))
os.environ["DATABASE_URL"] = f"sqlite:///{tmp}/eval.db"          # isolated DB: never touches real data
os.environ["UPLOAD_DIR"] = str(tmp / "uploads")

from rapidfuzz import fuzz  # noqa: E402
from sqlalchemy import select  # noqa: E402

import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.extraction.llm import llm_available  # noqa: E402
from app.extraction.utils import compact, norm_text  # noqa: E402
from app.models import Chunk, Document, Invoice  # noqa: E402
from app.pipeline import process_document  # noqa: E402
from app.processing.readers import sniff_type  # noqa: E402
from app.search import Embedder, embedder, search  # noqa: E402
from evaluation.run_scale import scale_lines  # noqa: E402
from evaluation.synth import FIELDS, build_dataset  # noqa: E402

# Queries that share no words with the invoices they should find, so only meaning can connect them.
SEMANTIC_PROBES = [
    ("furniture for staff", ["Office chair"], []), ("stationery for the classroom", ["Whiteboard markers"], []), ("ink for the copier", ["Printer toner"], []),
    ("protective wear for factory workers", ["Safety gloves"], []), ("computer networking hardware", ["Network switch"], []), ("web server rental", ["Cloud hosting plan"], []),
    ("advisory services billed by time", ["Consulting hour"], []), ("repairs and upkeep", ["Maintenance service"], []), ("shipping boxes", ["Cardboard cartons"], []),
    ("adhesive sealing supplies", ["Packing tape roll"], []), ("nuts and fasteners", ["Steel bolts (box)"], []), ("illumination fixtures", ["LED panel light"], []),
    ("charging cords for phones", ["USB-C cable"], []), ("monitor riser for a notebook computer", ["Laptop stand"], []), ("electrical conductor", ["Copper wire spool"], []),
    ("freight and delivery company", [], ["Blue Harbor Logistics"]), ("event food provider", [], ["Lakeside Catering Services"]), ("hospital equipment vendor", [], ["Summit Medical Supply"]),
    ("programming and IT firm", [], ["Vertex Software Solutions", "Orion Digital Services"]), ("clothing fabric manufacturer", [], ["Crescent Textiles Ltd"]),
]

data_dir = Path(args.data) if args.data else tmp / "dataset"
labels = json.loads((data_dir / "labels.json").read_text()) if args.data else build_dataset(data_dir, args.seed)
init_db()


def field_ok(k, t, p):
    if k == "line_items":
        return line_score(t or [], p or [])
    if t is None or p is None:
        return float(t is None and p is None)
    if k in ("subtotal", "tax", "total"):
        return float(abs(t - p) <= 0.01)
    if k == "invoice_number":
        return float(compact(t) == compact(p))
    if k in ("supplier_name", "customer_name"):
        return float(norm_text(t) == norm_text(p))
    return float(t == p)


def line_score(t, p):
    if not t and not p:
        return 1.0
    used, hit = set(), 0
    for a in t:
        for j, b in enumerate(p):
            if j in used:
                continue
            if fuzz.ratio(norm_text(a["description"] or ""), norm_text(b.get("description") or "")) >= 90 and \
                    abs((a["amount"] or 0) - (b.get("amount") or 0)) <= 0.01:
                used.add(j); hit += 1
                break
    return hit / max(len(t), len(p))


# ---------------------------------------------------------------- run
wall0 = time.perf_counter()
doc_ids = {}
for lab in labels:                                   # sequential + ordered => deterministic duplicate detection, clean timings
    path = data_dir / lab["file"]
    with session_scope() as s:
        d = Document(filename=lab["file"], stored_path=str(path), file_type=sniff_type(path.read_bytes()[:2048], lab["file"]),
                     size_bytes=path.stat().st_size, sha256="x")
        s.add(d); s.flush(); doc_ids[lab["file"]] = d.id
    process_document(doc_ids[lab["file"]])
wall = time.perf_counter() - wall0

per_kind = defaultdict(lambda: defaultdict(list))
field_scores = defaultdict(list)
exact, n_inv_truth, missed = [], 0, 0
halluc = [0, 0]
times, ms_per_page = [], []
ok_docs = failed_docs = 0
corrupt_graceful = corrupt_total = 0
flag_hit, flag_total, fp, fp_total, wrong_caught, wrong_total = 0, 0, 0, 0, 0, 0
CLEAN_KINDS = {"pdf_digital", "pdf_multi_invoice", "pdf_scanned", "image_good", "image_poor", "image_tiff_multipage", "xlsx", "csv"}
fail_examples, wrong_examples = [], []

with session_scope() as s:
    for lab in labels:
        d = s.get(Document, doc_ids[lab["file"]])
        preds = list(d.invoices)
        if lab["expect"] == "fail":
            corrupt_total += 1
            corrupt_graceful += int(d.status == "failed" and bool(d.error))
            continue
        if d.status == "failed":
            failed_docs += 1
            fail_examples.append(f"{lab['file']}: {d.error}")
        else:
            ok_docs += 1
        if d.duration_ms is not None:
            times.append(d.duration_ms)
            if d.page_count and d.file_type in ("pdf", "image"):
                ms_per_page.append(d.duration_ms / d.page_count)
        used = set()
        for t in lab["invoices"]:
            n_inv_truth += 1
            tf = t["fields"]
            m = next((p for p in preds if p.id not in used and p.invoice_number and compact(p.invoice_number) == compact(tf["invoice_number"])), None) \
                or next((p for p in preds if p.id not in used), None)
            if m is None:
                missed += 1
                for k in FIELDS:
                    field_scores[k].append(0.0); per_kind[lab["kind"]][k].append(0.0)
                exact.append(0.0)
                continue
            used.add(m.id)
            sc = {k: field_ok(k, tf[k], getattr(m, k)) for k in FIELDS}
            for k, v in sc.items():
                field_scores[k].append(v); per_kind[lab["kind"]][k].append(v)
                if tf[k] is None and k != "line_items":
                    halluc[1] += 1; halluc[0] += int(getattr(m, k) is not None)
            exact.append(float(all(v == 1.0 for v in sc.values())))
            codes = {i.code for i in m.issues}
            for f in t["flags"]:
                flag_total += 1; flag_hit += int(f in codes)
            if lab["kind"] in CLEAN_KINDS:
                needs_review = int(m.review_status == "needs_review")
                if all(v == 1.0 for v in sc.values()):        # correct and still sent to a person: a false alarm
                    fp_total += 1; fp += needs_review
                else:                                         # something is wrong: did a person get to see it?
                    wrong_total += 1; wrong_caught += needs_review
                    wrong_examples.append({"file": lab["file"], "flagged": bool(needs_review), "wrong": {
                        k: {"truth": tf[k], "read": getattr(m, k)} for k, v in sc.items() if v < 1.0}})

    # ---- search quality: can we find the document again?
    probes = [(l, t) for l in labels if l["kind"] in ("pdf_digital", "pdf_scanned", "image_good") for t in l["invoices"]][:15]
    kw_hit = sem_hit = num_hit = 0
    for lab, t in probes:
        did = doc_ids[lab["file"]]
        f = t["fields"]
        kw_hit += int(did in {h["document_id"] for h in search(s, f["supplier_name"], "keyword", limit=50)})
        num_hit += int(did in {h["document_id"] for h in search(s, f["invoice_number"], "keyword", limit=50)})
        sem_hit += int(did in {h["document_id"] for h in search(s, f"invoice from {f['supplier_name']} to {f['customer_name']}", "semantic", limit=5, min_score=0.0)})
    emb_name = embedder().name

    # ---- semantic quality: paraphrased queries, ranked over whole documents
    def rank_quality(rank):
        hits, rr, prec, n = 0, 0.0, 0.0, 0
        for query, items, suppliers in SEMANTIC_PROBES:
            relevant = {doc_ids[l["file"]] for l in labels if l["expect"] != "fail" for t in l["invoices"]
                        if t["fields"]["supplier_name"] in suppliers or any(i["description"] in items for i in t["fields"]["line_items"])}
            if not relevant:
                continue
            ranked = rank(query)
            first = next((k for k, d in enumerate(ranked) if d in relevant), None)
            n += 1
            hits += int(first is not None and first < 5)
            rr += 1 / (first + 1) if first is not None else 0.0
            prec += sum(d in relevant for d in ranked[:5]) / min(5, len(relevant))
        return {"hit@5": hits / max(1, n), "mrr": rr / max(1, n), "recall_top5": prec / max(1, n), "probes": n}

    semantic = rank_quality(lambda q: [h["document_id"] for h in search(s, q, "semantic", limit=100, min_score=0.0)])
    baseline = None
    if embedder().semantic:                                   # the same queries through the text-pattern fallback
        rows = s.execute(select(Chunk.document_id, Chunk.text)).all()
        fallback = Embedder("hash")
        mat, owners = fallback.encode([r.text for r in rows]), [r.document_id for r in rows]

        def rank_fallback(q):
            scores = mat @ fallback.encode([q])[0]
            return list(dict.fromkeys(owners[k] for k in np.argsort(-scores)))

        baseline = rank_quality(rank_fallback)

# ---------------------------------------------------------------- report
def pct(x):
    return f"{100 * x:.1f}%"


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else float("nan")


try:
    tv = subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
except Exception:
    tv = "n/a"
n_ok_files = ok_docs + failed_docs
res = {
    "files_total": len(labels), "files_expected_ok": n_ok_files, "invoices_in_truth": n_inv_truth, "invoices_missed": missed,
    "field_accuracy": {k: mean(v) for k, v in field_scores.items()},
    "overall_field_accuracy": mean([x for v in field_scores.values() for x in v]),
    "invoice_exact_match": mean(exact), "hallucination_rate": halluc[0] / max(1, halluc[1]),
    "doc_success_rate": ok_docs / max(1, n_ok_files), "doc_failure_rate": failed_docs / max(1, n_ok_files),
    "corrupt_handled_gracefully": f"{corrupt_graceful}/{corrupt_total}",
    "time_ms_mean": mean(times), "time_ms_p50": q(times, .5), "time_ms_p95": q(times, .95), "ms_per_page_mean": mean(ms_per_page),
    "total_wall_s": wall, "throughput_docs_per_min": 60 * len(labels) / wall,
    "validation_flag_recall": flag_hit / max(1, flag_total), "false_review_rate_on_clean": fp / max(1, fp_total),
    "wrong_extractions_sent_to_review": wrong_caught / max(1, wrong_total), "wrong_extractions": wrong_examples,
    "search": {"keyword_supplier_hit": kw_hit / len(probes), "keyword_invoice_no_hit": num_hit / len(probes), "name_phrase_hit@5": sem_hit / len(probes), "probes": len(probes),
               "embedder": emb_name, "paraphrased": semantic, "paraphrased_text_pattern_baseline": baseline},
}
out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
(out / "results.json").write_text(json.dumps(res, indent=1, default=float))
kinds = sorted(per_kind)
lines = [
    "# Evaluation report", "",
    f"*Generated by `python -m evaluation.run_eval` on {time.strftime('%Y-%m-%d %H:%M')} — all numbers below are measured, not estimated.*", "",
    "## Setup", "",
    f"- Dataset: **{len(labels)} files** / **{n_inv_truth} labelled invoices** ({'user-supplied' if args.data else f'synthetic, seed {args.seed}'}); kinds: " + ", ".join(kinds + ["corrupted"]),
    f"- Extraction mode: **{'LLM (' + settings.LLM_MODEL + ')' if (llm_available() and settings.EXTRACTION_MODE != 'rules') else 'rules-only (no ANTHROPIC_API_KEY set)'}**",
    f"- OCR: {tv}, langs `{settings.OCR_LANGS}`, psm {settings.OCR_PSM}; embeddings: `{emb_name}`",
    f"- Machine: Python {platform.python_version()}, {os.cpu_count()} CPU cores, sequential single-document processing", "",
    "## Headline metrics", "", "| Metric | Value |", "|---|---|",
    f"| Overall field accuracy (9 fields x all invoices) | **{pct(res['overall_field_accuracy'])}** |",
    f"| Invoices with every field correct | **{pct(res['invoice_exact_match'])}** |",
    f"| Invoices not found at all | {missed} / {n_inv_truth} |",
    f"| Hallucination rate (value returned where the document has none) | **{pct(res['hallucination_rate'])}** ({halluc[0]}/{halluc[1]}) |",
    f"| Document success rate (non-corrupt files) | **{pct(res['doc_success_rate'])}** ({ok_docs}/{n_ok_files}) |",
    f"| Document failure rate (non-corrupt files) | **{pct(res['doc_failure_rate'])}** ({failed_docs}/{n_ok_files}) |",
    f"| Corrupted files failed gracefully (clear error, no crash) | **{res['corrupt_handled_gracefully']}** |",
    f"| Processing time per document: mean / p50 / p95 | {res['time_ms_mean']:.0f} / {res['time_ms_p50']:.0f} / {res['time_ms_p95']:.0f} ms |",
    f"| Mean time per PDF/image page | {res['ms_per_page_mean']:.0f} ms |",
    f"| Throughput (1 worker, end-to-end incl. OCR) | {res['throughput_docs_per_min']:.1f} docs/min |",
    f"| Validation: injected errors detected (recall) | **{pct(res['validation_flag_recall'])}** ({flag_hit}/{flag_total}) |",
    f"| Validation: correct invoices wrongly sent to review | **{pct(res['false_review_rate_on_clean'])}** ({fp}/{fp_total}) |",
    f"| Validation: invoices with a wrong field sent to review | {pct(res['wrong_extractions_sent_to_review'])} ({wrong_caught}/{wrong_total}) |",
    f"| Search: keyword by supplier / by invoice no. (hit) | {pct(res['search']['keyword_supplier_hit'])} / {pct(res['search']['keyword_invoice_no_hit'])} |",
    f"| Search: lookup by supplier and customer name, top-5 hit | {pct(res['search']['name_phrase_hit@5'])} |",
    f"| Search: paraphrased queries sharing no words with the invoice, MRR / top-5 hit (`{emb_name}`) | **{semantic['mrr']:.2f}** / {pct(semantic['hit@5'])} ({semantic['probes']} queries) |",
    *([f"| Same queries through the text-pattern fallback, MRR / top-5 hit | {baseline['mrr']:.2f} / {pct(baseline['hit@5'])} |"] if baseline else []), "",
    "## Field accuracy by file kind", "", "| Kind | " + " | ".join(FIELDS) + " | n |", "|---|" + "---|" * (len(FIELDS) + 1)]
for kd in kinds:
    lines.append(f"| {kd} | " + " | ".join(pct(mean(per_kind[kd][k])) for k in FIELDS) + f" | {len(per_kind[kd]['total'])} |")
lines += ["", "## Field accuracy (all kinds)", "", "| Field | Accuracy |", "|---|---|"] + [f"| {k} | {pct(res['field_accuracy'][k])} |" for k in FIELDS]
if fail_examples:
    lines += ["", "## Failed documents", ""] + [f"- {e}" for e in fail_examples]
if (out / "scale.json").exists():                             # measured separately: python -m evaluation.run_scale
    lines += [""] + scale_lines(json.loads((out / "scale.json").read_text()))
lines += ["", "## How to read these numbers (limitations)", "",
          "- The dataset is **synthetic and generated by the same team that wrote the rules extractor**, so rules-mode accuracy here is optimistic. "
          "Treat it as a regression benchmark, not a forecast for real supplier invoices.",
          "- Scans are *simulated* (rotation, blur, noise, fading, JPEG artefacts) from rendered PDFs, not photographed paper.",
          "- With `ANTHROPIC_API_KEY` set the same harness measures the LLM path; with `--data` it measures your own labelled files.",
          "- Semantic search runs on the `fastembed` model named above. If the model cannot load, search falls back to a char-n-gram hashing embedder (fuzzy lexical, not truly semantic); this report and `/api/health` name the one in use.",
          "- The search corpus is small, so top-5 hit rates are generous for any method. MRR shows how close to the top the right document lands, and is the fairer comparison.",
          "- Timing is sequential on this machine; the API runs several workers in parallel (see README for scaling)."]
(out / "EVAL_REPORT.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
