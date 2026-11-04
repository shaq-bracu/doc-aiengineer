"""Keyword filtering + semantic search over chunk embeddings (hybrid)."""
import logging
import threading
from typing import Optional

import numpy as np
from sqlalchemy import func, or_, select, update

from .config import settings
from .db import session_scope
from .models import Chunk, Document, Invoice

log = logging.getLogger("search")
_emb = None
_emb_lock = threading.Lock()


class Embedder:
    def __init__(self, backend: Optional[str] = None):
        backend = backend or settings.EMBEDDING_BACKEND
        self.name, self._model, self._hv = None, None, None
        self.min_score = 0.15
        self.window = None            # semantic matches kept within this distance of the best one
        if backend in ("auto", "fastembed"):
            try:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(settings.EMBEDDING_MODEL, threads=settings.EMBEDDING_THREADS)
                self.name = f"fastembed:{settings.EMBEDDING_MODEL}"[:80]
                self.min_score = 0.3
                self.window = 0.15        # measured: relevant invoices sit near the top score, the long tail far below it
            except Exception as e:
                if backend == "fastembed":
                    raise
                log.warning("Semantic model unavailable, search falls back to text patterns: %s", e)
        if self._model is None:  # dependency-free fallback: char n-gram hashing (lexical/fuzzy, NOT truly semantic)
            from sklearn.feature_extraction.text import HashingVectorizer
            self._hv = HashingVectorizer(n_features=768, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False, norm="l2")
            self.name = "hash-char-ngram-768"

    @property
    def semantic(self) -> bool:
        return self._model is not None

    def encode(self, texts: list[str]) -> np.ndarray:
        if self._model is not None:
            v = np.array(list(self._model.embed(texts, batch_size=32)), dtype=np.float32)
            return v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)
        return self._hv.transform(texts).toarray().astype(np.float32)


def embedder() -> Embedder:
    global _emb
    with _emb_lock:
        if _emb is None:
            _emb = Embedder()
        return _emb


def embedder_status() -> dict:
    """For the health endpoint: never blocks on a model that is still loading."""
    return {"search_embedder": _emb.name if _emb else "loading", "semantic_search": bool(_emb and _emb.semantic)}


def chunk_text(text: str, size: int = 900) -> list[str]:
    chunks, buf = [], []
    n = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        buf.append(line.strip())
        n += len(line)
        if n >= size:
            chunks.append("\n".join(buf))
            buf, n = buf[-1:], len(buf[-1])  # one-line overlap
    if buf and (not chunks or len(buf) > 1):
        chunks.append("\n".join(buf))
    return chunks


def invoice_summary(inv: Invoice) -> str:
    return (f"Invoice {inv.invoice_number or '?'} from {inv.supplier_name or 'unknown supplier'} to {inv.customer_name or 'unknown customer'} "
            f"dated {inv.invoice_date or '?'} total {inv.total if inv.total is not None else '?'} {inv.currency or ''}").strip()


def line_items_text(inv: Invoice) -> Optional[str]:
    """A chunk of just the goods and services, so 'furniture' finds 'Office chair' without an invoice's numbers diluting it."""
    names = [i["description"] for i in inv.line_items or [] if i.get("description")]
    return "Line items: " + "; ".join(names) if names else None


def index_document(session, doc: Document, pages: list[tuple[int, str]], invoices: list[Invoice]) -> int:
    """Without `pages` (a manual correction) only the invoice chunks are rebuilt; the page text has not changed."""
    stale = session.query(Chunk).filter(Chunk.document_id == doc.id)
    if not pages:
        stale = stale.filter(Chunk.invoice_id.is_not(None))
    stale.delete()
    rows = []
    for inv in invoices:
        rows.append((None, inv.id, invoice_summary(inv)))
        items = line_items_text(inv)
        if items:
            rows.append((None, inv.id, items))
    for pno, text in pages:
        rows += [(pno, None, c) for c in chunk_text(text)]
    rows = rows[:5000]
    if not rows:
        return 0
    emb = embedder()
    for i in range(0, len(rows), 64):
        batch = rows[i:i + 64]
        vecs = emb.encode([r[2] for r in batch])
        session.add_all(Chunk(document_id=doc.id, invoice_id=r[1], page=r[0], text=r[2], emb_model=emb.name,
                              embedding=v.astype(np.float32).tobytes()) for r, v in zip(batch, vecs))
    return len(rows)


def reindex_stale_chunks(batch: int = 256) -> int:
    """Re-embed chunks written by another model, so changing models never hides existing documents from search."""
    emb = embedder()
    done = 0
    while True:
        with session_scope() as s:
            rows = s.execute(select(Chunk.id, Chunk.text).where(Chunk.emb_model != emb.name).limit(batch)).all()
            if not rows:
                return done
            vecs = emb.encode([r.text for r in rows])
            for r, v in zip(rows, vecs):
                s.execute(update(Chunk).where(Chunk.id == r.id).values(emb_model=emb.name, embedding=v.astype(np.float32).tobytes()))
        done += len(rows)


def add_missing_line_item_chunks(batch: int = 64) -> int:
    """Documents indexed before line-item chunks existed get them now, so search by meaning can reach their goods and services."""
    emb = embedder()
    covered = select(Chunk.invoice_id).where(Chunk.invoice_id.is_not(None), Chunk.text.like("Line items:%"))
    with session_scope() as s:
        rows = [(inv, text) for inv in s.scalars(select(Invoice).where(Invoice.id.not_in(covered))) if (text := line_items_text(inv))]
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            vecs = emb.encode([text for _, text in part])
            s.add_all(Chunk(document_id=inv.document_id, invoice_id=inv.id, text=text, emb_model=emb.name, embedding=v.astype(np.float32).tobytes())
                      for (inv, text), v in zip(part, vecs))
    return len(rows)


_cache = {"key": None, "ids": None, "docs": None, "mat": None}


def _matrix(session):
    name = embedder().name
    current = Chunk.emb_model == name
    key = (name, session.scalar(select(func.count(Chunk.id)).where(current)), session.scalar(select(func.max(Chunk.id)).where(current)))
    if _cache["key"] != key:
        rows = session.execute(select(Chunk.id, Chunk.document_id, Chunk.embedding).where(Chunk.emb_model == name)).all()
        if rows:
            _cache.update(ids=np.array([r[0] for r in rows]), docs=np.array([r[1] for r in rows]),
                          mat=np.vstack([np.frombuffer(r[2], dtype=np.float32) for r in rows]))
        else:
            _cache.update(ids=np.array([]), docs=np.array([]), mat=None)
        _cache["key"] = key
    return _cache


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def filtered_stmt(q: Optional[str] = None, **f):
    stmt = select(Document).outerjoin(Invoice, Invoice.document_id == Document.id)
    for term in (q or "").split():
        like = f"%{_esc(term)}%"
        stmt = stmt.where(or_(Document.filename.ilike(like, escape="\\"), Document.extracted_text.ilike(like, escape="\\"),
                              Invoice.invoice_number.ilike(like, escape="\\"), Invoice.supplier_name.ilike(like, escape="\\"),
                              Invoice.customer_name.ilike(like, escape="\\")))
    for col, key in ((Invoice.supplier_name, "supplier"), (Invoice.customer_name, "customer"), (Invoice.invoice_number, "invoice_number")):
        if f.get(key):
            stmt = stmt.where(col.ilike(f"%{_esc(f[key])}%", escape="\\"))
    if f.get("currency"):
        stmt = stmt.where(Invoice.currency == f["currency"].upper())
    if f.get("date_from"):
        stmt = stmt.where(Invoice.invoice_date >= f["date_from"])
    if f.get("date_to"):
        stmt = stmt.where(Invoice.invoice_date <= f["date_to"])
    if f.get("min_total") is not None:
        stmt = stmt.where(Invoice.total >= f["min_total"])
    if f.get("max_total") is not None:
        stmt = stmt.where(Invoice.total <= f["max_total"])
    if f.get("review_status"):
        stmt = stmt.where(Invoice.review_status == f["review_status"])
    if f.get("status"):
        stmt = stmt.where(Document.status == f["status"])
    if f.get("file_type"):
        stmt = stmt.where(Document.file_type == f["file_type"])
    return stmt.distinct()


def search(session, q: str, mode: str = "hybrid", limit: int = 20, offset: int = 0, min_score: Optional[float] = None, **filters):
    """-> list of {document_id, score, match, snippet, page}; caller hydrates documents."""
    window = None
    if min_score is None:                   # interactive search; evaluations pass min_score to see the full ranking
        min_score, window = embedder().min_score, embedder().window
    hits: dict[int, dict] = {}
    stmt = filtered_stmt(q if mode in ("keyword", "hybrid") else None, **filters)
    if mode in ("keyword", "hybrid"):
        for d in session.scalars(stmt.order_by(Document.id.desc()).limit(500)):
            hits[d.id] = {"document_id": d.id, "score": 1.0, "match": "keyword", "snippet": None, "page": None}
    if mode in ("semantic", "hybrid") and q.strip():
        allowed = None
        if any(v not in (None, "") for v in filters.values()):
            allowed = set(session.scalars(stmt.with_only_columns(Document.id)))
        m = _matrix(session)
        if m["mat"] is not None:
            qv = embedder().encode([q])[0]
            sims = m["mat"] @ qv
            best: dict[int, tuple[float, int]] = {}
            for idx in np.argsort(-sims)[:300]:
                did, s = int(m["docs"][idx]), float(sims[idx])
                if s < min_score or (allowed is not None and did not in allowed):
                    continue
                if did not in best:
                    best[did] = (s, int(m["ids"][idx]))
            if window and best:
                top = max(s for s, _ in best.values())
                best = {did: v for did, v in best.items() if v[0] >= top - window}
            for did, (s, cid) in best.items():
                if did in hits:
                    hits[did]["score"] += s
                    hits[did]["match"] = "keyword+semantic"
                else:
                    hits[did] = {"document_id": did, "score": s, "match": "semantic", "snippet": None, "page": None}
                hits[did]["_chunk"] = cid
    ranked = sorted(hits.values(), key=lambda h: -h["score"])[offset:offset + limit]
    for h in ranked:
        cid = h.pop("_chunk", None)
        if cid:
            c = session.get(Chunk, cid)
            h["snippet"], h["page"] = c.text[:300], c.page
    return ranked
