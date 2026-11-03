from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Document(Base):
    """One uploaded file + its processing state."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(10))          # pdf | image | xlsx | xls | csv
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")  # queued|processing|completed|needs_review|failed
    stage: Mapped[str] = mapped_column(String(30), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    mean_ocr_conf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str] = mapped_column(Text, default="", deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    invoices = relationship("Invoice", back_populates="document", cascade="all, delete-orphan", order_by="Invoice.id")
    events = relationship("ProcessingEvent", cascade="all, delete-orphan", order_by="ProcessingEvent.id")
    issues = relationship("ValidationIssue", cascade="all, delete-orphan", order_by="ValidationIssue.id",
                          overlaps="invoice,issues")
    chunks = relationship("Chunk", cascade="all, delete-orphan")


class Invoice(Base):
    """One extracted invoice record. A document may hold many (multi-invoice PDFs, spreadsheets)."""
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)   # e.g. "Sheet1!row 5"
    invoice_number: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    invoice_date: Mapped[Optional[str]] = mapped_column(String(10), index=True, nullable=True)  # ISO yyyy-mm-dd
    supplier_name: Mapped[Optional[str]] = mapped_column(String(255), index=True, nullable=True)
    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subtotal: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tax: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    line_items: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    extraction_notes: Mapped[list] = mapped_column(JSON, default=list)
    method: Mapped[str] = mapped_column(String(30), default="rules")
    review_status: Mapped[str] = mapped_column(String(20), default="ok", index=True)  # ok|needs_review|approved
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(80), index=True, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="", deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    document = relationship("Document", back_populates="invoices")
    issues = relationship("ValidationIssue", back_populates="invoice", cascade="all, delete-orphan",
                          order_by="ValidationIssue.id", overlaps="issues")


class ValidationIssue(Base):
    __tablename__ = "validation_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=True)
    code: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(10))   # error | warning | info
    field: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    invoice = relationship("Invoice", back_populates="issues", overlaps="issues")


class ProcessingEvent(Base):
    """Append-only processing history (audit trail)."""
    __tablename__ = "processing_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Chunk(Base):
    """Searchable text chunk + embedding (float32 bytes). Swap for pgvector/Qdrant at scale."""
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    emb_model: Mapped[str] = mapped_column(String(80), index=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
