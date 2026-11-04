"""Background processing pipeline + in-process job queue (crash-safe: state lives in the DB)."""
import logging
import queue
import re
import threading
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import delete, select

from .config import settings
from .db import session_scope
from .extraction import rules
from .extraction.extractor import extract_invoice
from .extraction.tabular import extract_table
from .extraction.utils import fold_lookalikes, norm_text
from .models import Chunk, Document, Invoice, ProcessingEvent, ValidationIssue, utcnow
from .processing.readers import CorruptFileError, Page, read_image, read_pdf, read_spreadsheet
from .search import index_document
from .validation import apply_validation, dedupe_key

log = logging.getLogger("pipeline")
_VALIDATION_LOCK = threading.Lock()   # makes duplicate detection deterministic under concurrent workers
MIN_CLEAN_READS = 3                   # readings needed before a batch can vouch for a look-alike number
REPAIRED_CONFIDENCE = 0.75


def log_event(s, doc, stage, msg, level="info", ms=None):
    s.add(ProcessingEvent(document_id=doc.id, stage=stage, level=level, message=msg[:2000], duration_ms=ms))
    s.commit()


def set_progress(s, doc, stage, progress):
    doc.stage, doc.progress = stage, round(progress, 3)
    s.commit()


def segment_pages(pages: list[Page]) -> list[list[Page]]:
    """Split a multi-page PDF into invoices: a new invoice starts when a page shows a different invoice number,
    or carries an invoice-number label whose value could not be read (merging it would mix two invoices' fields)."""
    same = lambda a, b: fold_lookalikes(re.sub(r"\W", "", a).lower()) == fold_lookalikes(re.sub(r"\W", "", b).lower())
    segs: list[list[Page]] = []
    cur_no, cur_labelled = None, False
    for p in pages:
        no = rules.find_invoice_number(p.text) if p.text else None
        labelled = bool(no) or (bool(p.text) and rules.has_invoice_label(p.text))
        if not segs or (labelled and cur_labelled and not (no and cur_no and same(no, cur_no))):
            segs.append([p])
            cur_no, cur_labelled = no, labelled
        else:
            segs[-1].append(p)
            cur_no, cur_labelled = cur_no or no, cur_labelled or labelled
    return segs


def _split_serial(number: str) -> tuple[str, str]:
    """'SI/2025/00020' -> ('SI/2025/', '00020'): the part of a number that stays the same, and its running serial."""
    m = re.fullmatch(r"(.*\D)?(\d+)", number)
    return (m.group(1) or "", m.group(2)) if m else (number, "")


def reconcile_lookalike_numbers(invoices: list[Invoice]) -> None:
    """Inside one file, repair numbers OCR may have misread when the supplier's other invoices settle the question.

    OCR turns a real SI/ into S1/ often, but a real S1/ was never read as SI/ when this was measured. So a clear majority
    of clean readings from the same supplier is real evidence. Without it the number keeps its review flag."""
    groups = defaultdict(list)
    for inv in invoices:
        if inv.invoice_number and inv.supplier_name:
            prefix, serial = _split_serial(inv.invoice_number)
            if prefix:
                groups[norm_text(inv.supplier_name), fold_lookalikes(prefix)].append((inv, prefix, serial))
    for members in groups.values():
        flagged = [m for m in members if "lookalike:invoice_number" in (m[0].extraction_notes or [])]
        clean = Counter(m[1] for m in members if m not in flagged)
        if not flagged or not clean:
            continue
        best, reads = clean.most_common(1)[0]
        if reads < MIN_CLEAN_READS or reads <= len(members) - reads:
            continue
        width = Counter(len(m[2]) for m in members if m not in flagged).most_common(1)[0][0]
        for inv, _, serial in flagged:
            if len(serial) != width:          # e.g. '0091 1' cut to '0091': the serial is damaged too, so a person decides
                continue
            inv.invoice_number = best + serial
            inv.dedupe_key = dedupe_key(inv.invoice_number)
            inv.confidence = {**inv.confidence, "invoice_number": REPAIRED_CONFIDENCE}
            inv.extraction_notes = [n for n in inv.extraction_notes if n != "lookalike:invoice_number"] + ["repaired:invoice_number"]


def _new_invoice(doc, idx, res, **kw) -> Invoice:
    f = res["fields"]
    return Invoice(document_id=doc.id, segment_index=idx, method=res["method"], confidence=res["confidence"],
                   extraction_notes=res["notes"], dedupe_key=dedupe_key(f.get("invoice_number")),
                   **{k: f.get(k) for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency")},
                   line_items=f.get("line_items") or [], **kw)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _run(s, doc: Document, t_start: float):
    path = Path(doc.stored_path)
    t = time.perf_counter()
    set_progress(s, doc, "reading", 0.02)
    last = [0.0]

    def prog(done, total):
        if time.monotonic() - last[0] > 1.0 or done == total:
            last[0] = time.monotonic()
            doc.progress = round(0.02 + 0.58 * done / total, 3)
            s.commit()

    pages: list[Page] = []
    sheets = []
    if doc.file_type == "pdf":
        pages = read_pdf(path, prog)
    elif doc.file_type == "image":
        pages = read_image(path, prog)
    else:
        sheets = read_spreadsheet(path, doc.file_type)
    doc.page_count = len(pages) or len(sheets)
    doc.ocr_pages = sum(1 for p in pages if p.method == "ocr")
    doc.mean_ocr_conf = _mean([p.ocr_conf for p in pages])
    failed = [p for p in pages if p.method == "failed"]
    if pages and len(failed) == len(pages):
        raise CorruptFileError(f"No page could be read. First error: {failed[0].error}")
    if failed:
        msg = f"{len(failed)} of {len(pages)} pages could not be read (pages {[p.number for p in failed][:20]}): {failed[0].error}"
        s.add(ValidationIssue(document_id=doc.id, code="page_failed", severity="error", message=msg))
        log_event(s, doc, "reading", msg, "warning")
    if doc.mean_ocr_conf is not None and doc.mean_ocr_conf < 60:
        s.add(ValidationIssue(document_id=doc.id, code="low_ocr_confidence", severity="warning",
                              message=f"Mean OCR confidence is {doc.mean_ocr_conf:.0f}/100; check extracted values carefully"))
    doc.extracted_text = ("\n\n".join(f"--- page {p.number} ---\n{p.text}" for p in pages) if pages else "\n\n".join(sh.text for sh in sheets))[:5_000_000]
    s.commit()
    log_event(s, doc, "reading", f"Read {doc.page_count} {'pages' if pages else 'sheets'} ({doc.ocr_pages} via OCR)", ms=int((time.perf_counter() - t) * 1000))

    t = time.perf_counter()
    set_progress(s, doc, "extracting", 0.62)
    invoices: list[Invoice] = []
    if pages:
        segs = [sg for sg in segment_pages(pages) if any(p.text.strip() for p in sg)]
        for i, seg in enumerate(segs):
            text = "\n".join(p.text for p in seg)
            res = extract_invoice(text, _mean([p.ocr_conf for p in seg]))
            invoices.append(_new_invoice(doc, i, res, page_start=seg[0].number, page_end=seg[-1].number, raw_text=text[:100_000]))
            doc.progress = round(0.62 + 0.2 * (i + 1) / len(segs), 3)
        reconcile_lookalike_numbers(invoices)
    else:
        n = 0
        for sh in sheets:
            recs = extract_table(sh.df, sh.name)
            if recs:
                for r in recs:
                    invoices.append(_new_invoice(doc, n, r, source_ref=r["source_ref"], raw_text=r["raw_text"][:100_000]))
                    n += 1
            else:  # free-form sheet (e.g. a single invoice laid out like a form)
                res = extract_invoice(sh.text)
                if res["fields"]["total"] is not None or res["fields"]["invoice_number"]:
                    invoices.append(_new_invoice(doc, n, res, source_ref=sh.name, raw_text=sh.text[:100_000]))
                    n += 1
                else:
                    log_event(s, doc, "extracting", f"Sheet '{sh.name}' contains no invoice data; skipped")
    s.add_all(invoices)
    s.commit()
    if not invoices:
        s.add(ValidationIssue(document_id=doc.id, code="no_invoice_found", severity="error", message="No invoice data could be identified in this file"))
    log_event(s, doc, "extracting", f"Extracted {len(invoices)} invoice record(s) using {sorted({i.method for i in invoices}) or 'n/a'}", ms=int((time.perf_counter() - t) * 1000))

    t = time.perf_counter()
    set_progress(s, doc, "validating", 0.85)
    with _VALIDATION_LOCK:
        for inv in invoices:
            apply_validation(s, inv)
        s.commit()
    nrev = sum(1 for i in invoices if i.review_status == "needs_review")
    log_event(s, doc, "validating", f"{nrev} of {len(invoices)} record(s) need manual review", ms=int((time.perf_counter() - t) * 1000))

    t = time.perf_counter()
    set_progress(s, doc, "indexing", 0.93)
    try:
        nch = index_document(s, doc, [(p.number, p.text) for p in pages] or [(None, sh.text) for sh in sheets], invoices)
        s.commit()
        log_event(s, doc, "indexing", f"Indexed {nch} chunk(s)", ms=int((time.perf_counter() - t) * 1000))
    except Exception as e:  # search index failure must not fail extraction
        s.rollback()
        log_event(s, doc, "indexing", f"Indexing failed: {e}", "warning")

    refresh_status(s, doc)
    doc.progress, doc.stage = 1.0, "done"
    doc.finished_at = utcnow()
    doc.duration_ms = int((time.perf_counter() - t_start) * 1000)
    s.commit()


def refresh_status(s, doc: Document):
    s.expire(doc, ["invoices", "issues"])
    bad = any(i.review_status == "needs_review" for i in doc.invoices) or \
        any(i.invoice_id is None and i.severity == "error" and not i.resolved for i in doc.issues)
    doc.status = "needs_review" if bad else "completed"


def process_document(doc_id: int):
    t0 = time.perf_counter()
    with session_scope() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            return
        s.execute(delete(ValidationIssue).where(ValidationIssue.document_id == doc_id))
        s.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        s.execute(delete(Invoice).where(Invoice.document_id == doc_id))
        doc.status, doc.error, doc.progress, doc.stage = "processing", None, 0.0, "starting"
        doc.attempts += 1
        doc.started_at, doc.finished_at, doc.duration_ms = utcnow(), None, None
        s.commit()
        log_event(s, doc, "start", f"Processing attempt {doc.attempts}")
        try:
            _run(s, doc, t0)
        except Exception as e:
            s.rollback()
            doc = s.get(Document, doc_id)
            kind = "corrupt_or_unreadable" if isinstance(e, CorruptFileError) else "internal_error"
            doc.status, doc.stage = "failed", "failed"
            doc.error = f"{kind}: {e}"[:1000]
            doc.finished_at, doc.duration_ms = utcnow(), int((time.perf_counter() - t0) * 1000)
            s.commit()
            log_event(s, doc, "failed", doc.error + ("" if kind != "internal_error" else "\n" + traceback.format_exc()[-1500:]), "error")


class JobQueue:
    """In-process worker pool. State is in the DB, so unfinished jobs are re-queued on restart.
    For multi-node scale, swap this class for Celery/RQ/BullMQ consumers (same `process_document` entry point)."""

    def __init__(self):
        self.q: queue.Queue[int] = queue.Queue()
        self.inflight: set[int] = set()
        self.lock = threading.Lock()
        self.started = False

    def start(self, workers: int):
        if self.started:
            return
        self.started = True
        for i in range(workers):
            threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True).start()

    def enqueue(self, doc_id: int):
        with self.lock:
            if doc_id in self.inflight:
                return
            self.inflight.add(doc_id)
        self.q.put(doc_id)

    def _loop(self):
        while True:
            doc_id = self.q.get()
            try:
                process_document(doc_id)
            except Exception:
                log.exception("worker crashed on document %s", doc_id)
            finally:
                with self.lock:
                    self.inflight.discard(doc_id)
                self.q.task_done()

    def requeue_unfinished(self):
        with session_scope() as s:
            for did in s.scalars(select(Document.id).where(Document.status.in_(["queued", "processing"])).order_by(Document.id)):
                self.enqueue(did)

    def wait_idle(self):
        self.q.join()


queue_ = JobQueue()
