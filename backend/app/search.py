"""Keyword filtering + semantic search over chunk embeddings (hybrid)."""
import threading
from typing import Optional

import numpy as np
from sqlalchemy import func, or_, select

from .config import settings
from .models import Chunk, Document, Invoice

_emb = None
_emb_lock = threading.Lock()


class Embedder:
    def __init__(self):
        self.name, self._st, self._hv = None, None, None
        if settings.EMBEDDING_BACKEND in ("auto", "st"):
            try:
                from sentence_transformers import SentenceTransformer
                self._st = SentenceTransformer(settings.EMBEDDING_MODEL)
                self.name = f"st:{settings.EMBEDDING_MODEL}"[:80]
            except Exception:
                if settings.EMBEDDING_BACKEND == "st":
                    raise
        if self._st is None:  # dependency-free fallback: char n-gram hashing (lexical/fuzzy, NOT truly semantic)
            from sklearn.feature_extraction.text import HashingVectorizer
            self._hv = HashingVectorizer(n_features=768, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False, norm="l2")
            self.name = "hash-char-ngram-768"

    def encode(self, texts: list[str]) -> np.ndarray:
        if self._st is not None:
            return np.asarray(self._st.encode(texts, normalize_embeddings=True, batch_size=32), dtype=np.float32)
        return self._hv.transform(texts).toarray().astype(np.float32)


def embedder() -> Embedder:
    global _emb
    with _emb_lock:
        if _emb is None:
            _emb = Embedder()
        return _emb


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


def index_document(session, doc: Document, pages: list[tuple[int, str]], invoices: list[Invoice]) -> int:
    session.query(Chunk).filter(Chunk.document_id == doc.id).delete()
    rows = [(None, inv.id, invoice_summary(inv)) for inv in invoices]
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


_cache = {"key": None, "ids": None, "docs": None, "mat": None}


def _matrix(session):
    name = embedder().name
    key = (name, session.scalar(select(func.count(Chunk.id))), session.scalar(select(func.max(Chunk.id))))
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


def search(session, q: str, mode: str = "hybrid", limit: int = 20, offset: int = 0, min_score: float = 0.15, **filters):
    """-> list of {document_id, score, match, snippet, page}; caller hydrates documents."""
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
