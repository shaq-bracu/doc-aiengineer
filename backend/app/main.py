"""REST API. Uploads return immediately; OCR/AI work runs in background workers (app.pipeline)."""
import hashlib
import logging
import os
import re
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, init_db, session_scope
from .extraction.extractor import FIELDS
from .extraction.llm import llm_available
from .extraction.utils import normalize_date
from .models import Document, Invoice, ProcessingEvent, ValidationIssue
from .pipeline import log_event, queue_, refresh_status
from .processing.ocr import ocr_available
from .processing.readers import UnsupportedFileError, sniff_type
from .search import add_missing_line_item_chunks, embedder, embedder_status, filtered_stmt, index_document, reindex_stale_chunks, search as run_search
from .validation import apply_validation, dedupe_key, refresh_dedupe_keys

log = logging.getLogger("api")


def _startup_maintenance():
    """Load the search model before the first request needs it, and bring data from older versions up to date."""
    try:
        with session_scope() as s:
            refresh_dedupe_keys(s)
        n = reindex_stale_chunks() + add_missing_line_item_chunks()
        if n:
            log.info("Brought %d search chunks up to date for %s", n, embedder().name)
    except Exception:
        log.exception("Startup maintenance failed; it will run again on the next start")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    queue_.start(settings.WORKERS)
    queue_.requeue_unfinished()      # crash recovery
    threading.Thread(target=_startup_maintenance, name="maintenance", daemon=True).start()
    yield


app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


def require_key(x_api_key: str = Header(default="")):
    if settings.API_KEY and not secrets.compare_digest(x_api_key, settings.API_KEY):
        raise HTTPException(401, "Invalid or missing API key")


router = APIRouter(prefix="/api", dependencies=[Depends(require_key)])


# ------------------------------------------------------------------ serializers
def issue_dict(i: ValidationIssue):
    return {"id": i.id, "invoice_id": i.invoice_id, "code": i.code, "severity": i.severity, "field": i.field,
            "message": i.message, "resolved": i.resolved}


def inv_dict(i: Invoice, issues=True):
    conf = i.confidence or {}
    d = {"id": i.id, "document_id": i.document_id, "filename": i.document.filename if i.document else None,
         "segment_index": i.segment_index, "page_start": i.page_start, "page_end": i.page_end, "source_ref": i.source_ref,
         **{k: getattr(i, k) for k in FIELDS}, "confidence": conf, "method": i.method, "review_status": i.review_status,
         "edited": i.edited, "notes": i.extraction_notes or [],
         "uncertain_fields": [k for k in FIELDS if getattr(i, k) not in (None, []) and conf.get(k, 1.0) < settings.REVIEW_CONFIDENCE_THRESHOLD]}
    if issues:
        d["issues"] = [issue_dict(x) for x in i.issues]
    return d


def doc_dict(d: Document, detail=False):
    out = {"id": d.id, "filename": d.filename, "file_type": d.file_type, "size_bytes": d.size_bytes, "status": d.status,
           "stage": d.stage, "progress": d.progress, "page_count": d.page_count, "ocr_pages": d.ocr_pages,
           "mean_ocr_conf": d.mean_ocr_conf, "attempts": d.attempts, "error": d.error, "duration_ms": d.duration_ms,
           "created_at": d.created_at.isoformat(), "finished_at": d.finished_at.isoformat() if d.finished_at else None,
           "invoice_count": len(d.invoices)}
    if detail:
        out["invoices"] = [inv_dict(i) for i in d.invoices]
        out["document_issues"] = [issue_dict(i) for i in d.issues if i.invoice_id is None]
        out["events"] = [{"stage": e.stage, "level": e.level, "message": e.message, "duration_ms": e.duration_ms,
                          "at": e.created_at.isoformat()} for e in d.events]
    return out


# ------------------------------------------------------------------ upload
class Rejected(Exception):
    pass


def _safe_name(name: str) -> str:
    name = os.path.basename((name or "file").replace("\\", "/"))
    return re.sub(r"[\x00-\x1f]", "", name)[:200] or "file"


def _store(f: UploadFile, db: Session) -> Document:
    name = _safe_name(f.filename)
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    stored = settings.UPLOAD_DIR / (uuid.uuid4().hex + os.path.splitext(name)[1].lower())
    h, size, head = hashlib.sha256(), 0, b""
    try:
        with open(stored, "wb") as out:
            while chunk := f.file.read(1 << 20):
                if not head:
                    head = chunk[:2048]
                size += len(chunk)
                if size > limit:
                    raise Rejected(f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit")
                h.update(chunk)
                out.write(chunk)
        if size == 0:
            raise Rejected("File is empty")
        try:
            kind = sniff_type(head, name)
        except UnsupportedFileError as e:
            raise Rejected(str(e))
    except Exception:
        stored.unlink(missing_ok=True)
        raise
    doc = Document(filename=name, stored_path=str(stored), file_type=kind, size_bytes=size, sha256=h.hexdigest())
    db.add(doc)
    db.flush()
    dup = db.scalar(select(Document.id).where(Document.sha256 == doc.sha256, Document.id != doc.id))
    log_event(db, doc, "upload", f"Uploaded ({size} bytes)" + (f"; identical file content to document #{dup}" if dup else ""), "warning" if dup else "info")
    return doc


@router.post("/documents", status_code=201)
def upload_documents(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    if len(files) > settings.MAX_FILES_PER_REQUEST:
        raise HTTPException(413, f"Max {settings.MAX_FILES_PER_REQUEST} files per request")
    results, accepted = [], []
    for f in files:
        try:
            d = _store(f, db)
            db.commit()
            accepted.append(d.id)
            results.append({"filename": f.filename, "accepted": True, "document": doc_dict(d)})
        except Rejected as e:
            results.append({"filename": f.filename, "accepted": False, "error": str(e)})
    for i in accepted:
        queue_.enqueue(i)
    return JSONResponse(status_code=201 if accepted else 400, content={"results": results})


# ------------------------------------------------------------------ documents
def _doc_or_404(db, doc_id) -> Document:
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    return d


@router.get("/documents")
def list_documents(status: Optional[str] = None, q: Optional[str] = None, limit: int = Query(50, le=200), offset: int = 0, db: Session = Depends(get_db)):
    stmt = select(Document)
    if status:
        stmt = stmt.where(Document.status.in_(status.split(",")))
    if q:
        stmt = stmt.where(Document.filename.ilike(f"%{q}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Document.id.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [doc_dict(d) for d in rows]}


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db)):
    return doc_dict(_doc_or_404(db, doc_id), detail=True)


@router.get("/documents/{doc_id}/text")
def get_text(doc_id: int, db: Session = Depends(get_db)):
    d = _doc_or_404(db, doc_id)
    return {"document_id": d.id, "text": d.extracted_text}


@router.get("/documents/{doc_id}/file")
def get_file(doc_id: int, db: Session = Depends(get_db)):
    d = _doc_or_404(db, doc_id)
    return FileResponse(d.stored_path, filename=d.filename)


@router.post("/documents/reprocess-failed")
def reprocess_failed(db: Session = Depends(get_db)):
    ids = list(db.scalars(select(Document.id).where(Document.status == "failed")))
    for i in ids:
        d = db.get(Document, i)
        d.status, d.stage = "queued", "queued"
    db.commit()
    for i in ids:
        queue_.enqueue(i)
    return {"requeued": ids}


@router.post("/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: int, db: Session = Depends(get_db)):
    d = _doc_or_404(db, doc_id)
    if d.status in ("queued", "processing"):
        raise HTTPException(409, "Document is already being processed")
    d.status, d.stage = "queued", "queued"
    log_event(db, d, "reprocess", "Reprocessing requested")
    queue_.enqueue(d.id)
    return doc_dict(d)


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    d = _doc_or_404(db, doc_id)
    if d.status == "processing":
        raise HTTPException(409, "Document is being processed")
    try:
        os.unlink(d.stored_path)
    except OSError:
        pass
    db.delete(d)


# ------------------------------------------------------------------ invoices / corrections
class LineItemIn(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class InvoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    supplier_name: Optional[str] = None
    customer_name: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None
    line_items: Optional[list[LineItemIn]] = None


def _inv_or_404(db, inv_id) -> Invoice:
    i = db.get(Invoice, inv_id)
    if not i:
        raise HTTPException(404, "Invoice not found")
    return i


@router.get("/invoices")
def list_invoices(review_status: Optional[str] = None, limit: int = Query(100, le=500), offset: int = 0, db: Session = Depends(get_db)):
    stmt = select(Invoice)
    if review_status:
        stmt = stmt.where(Invoice.review_status == review_status)
    return {"items": [inv_dict(i) for i in db.scalars(stmt.order_by(Invoice.id.desc()).limit(limit).offset(offset))]}


@router.get("/invoices/{inv_id}")
def get_invoice(inv_id: int, db: Session = Depends(get_db)):
    i = _inv_or_404(db, inv_id)
    return {**inv_dict(i), "raw_text": i.raw_text}


@router.patch("/invoices/{inv_id}")
def correct_invoice(inv_id: int, patch: InvoicePatch, db: Session = Depends(get_db)):
    inv = _inv_or_404(db, inv_id)
    changed = []
    conf = dict(inv.confidence or {})
    for field in patch.model_fields_set:
        val = getattr(patch, field)
        if field == "invoice_date" and val not in (None, ""):
            iso, _ = normalize_date(val, settings.DATE_DAYFIRST)
            if not iso:
                raise HTTPException(422, "invoice_date must be a valid date (YYYY-MM-DD)")
            val = iso
        elif field == "currency" and val:
            val = val.strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", val):
                raise HTTPException(422, "currency must be a 3-letter ISO code")
        elif field == "line_items":
            val = [x.model_dump() for x in (val or [])]
        elif isinstance(val, str):
            val = val.strip() or None
        if getattr(inv, field) != val:
            setattr(inv, field, val)
            changed.append(field)
        conf[field] = 1.0 if val not in (None, []) else 0.0
    inv.confidence = conf
    inv.dedupe_key = dedupe_key(inv.invoice_number)
    inv.edited = inv.edited or bool(changed)
    inv.extraction_notes = [n for n in (inv.extraction_notes or []) if n.partition(":")[2] not in changed]
    db.flush()
    apply_validation(db, inv)
    doc = inv.document
    if changed:
        log_event(db, doc, "correction", f"Invoice #{inv.id}: manually corrected {', '.join(changed)}")
        try:
            index_document(db, doc, [], list(doc.invoices))  # refresh invoice summary chunks only
        except Exception:
            db.rollback()
    refresh_status(db, doc)
    return inv_dict(inv)


@router.post("/invoices/{inv_id}/approve")
def approve_invoice(inv_id: int, db: Session = Depends(get_db)):
    inv = _inv_or_404(db, inv_id)
    inv.review_status = "approved"
    for i in inv.issues:
        i.resolved = True
    log_event(db, inv.document, "approve", f"Invoice #{inv.id} approved by reviewer")
    refresh_status(db, inv.document)
    return inv_dict(inv)


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(issue_id: int, db: Session = Depends(get_db)):
    i = db.get(ValidationIssue, issue_id)
    if not i:
        raise HTTPException(404, "Issue not found")
    i.resolved = True
    db.flush()
    refresh_status(db, db.get(Document, i.document_id))
    return issue_dict(i)


# ------------------------------------------------------------------ search / metrics / health
@router.get("/search")
def search_documents(q: str = "", mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
                     supplier: Optional[str] = None, customer: Optional[str] = None, invoice_number: Optional[str] = None,
                     currency: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                     min_total: Optional[float] = None, max_total: Optional[float] = None, review_status: Optional[str] = None,
                     status: Optional[str] = None, file_type: Optional[str] = None,
                     limit: int = Query(20, le=100), offset: int = 0, db: Session = Depends(get_db)):
    hits = run_search(db, q, mode, limit, offset, supplier=supplier, customer=customer, invoice_number=invoice_number,
                      currency=currency, date_from=date_from, date_to=date_to, min_total=min_total, max_total=max_total,
                      review_status=review_status, status=status, file_type=file_type)
    items = []
    for h in hits:
        d = db.get(Document, h["document_id"])
        if d:
            items.append({**h, "document": doc_dict(d), "invoices": [inv_dict(i, issues=False) for i in d.invoices]})
    return {"query": q, "mode": mode, "count": len(items), "items": items}


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    by_status = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    finished = sum(by_status.get(k, 0) for k in ("completed", "needs_review", "failed"))
    ok = by_status.get("completed", 0) + by_status.get("needs_review", 0)
    n_inv = db.scalar(select(func.count(Invoice.id))) or 0
    fill = {}
    for f in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"):
        fill[f] = round((db.scalar(select(func.count(getattr(Invoice, f)))) or 0) / n_inv, 3) if n_inv else None
    return {"documents_by_status": by_status, "documents_total": sum(by_status.values()),
            "success_rate": round(ok / finished, 4) if finished else None, "failure_rate": round(by_status.get("failed", 0) / finished, 4) if finished else None,
            "avg_processing_ms": db.scalar(select(func.avg(Document.duration_ms)).where(Document.duration_ms.is_not(None))),
            "invoices_total": n_inv, "invoices_needing_review": db.scalar(select(func.count(Invoice.id)).where(Invoice.review_status == "needs_review")),
            "field_fill_rate": fill, "queue_depth": queue_.q.qsize()}


app.include_router(router)


@app.get("/api/health")
def health():
    return {"status": "ok", "ocr_available": ocr_available(), "llm_enabled": llm_available() and settings.EXTRACTION_MODE != "rules",
            "extraction_mode": settings.EXTRACTION_MODE, **embedder_status(), "workers": settings.WORKERS, "queue_depth": queue_.q.qsize()}
