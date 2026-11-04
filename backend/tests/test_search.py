import numpy as np
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Chunk, Document, Invoice
from app.search import Embedder, add_missing_line_item_chunks, embedder, reindex_stale_chunks, search
from app.validation import dedupe_key, refresh_dedupe_keys
from tests.conftest import upload, wait_done


def _chunks(doc_id):
    with SessionLocal() as s:
        return s.scalars(select(Chunk).where(Chunk.document_id == doc_id)).all()


def test_line_items_get_their_own_chunk(client, files):
    path, inv = files["digital"]
    _, ids = upload(client, path, name="items.pdf")
    wait_done(client, ids[0])
    chunks = [c.text for c in _chunks(ids[0]) if c.text.startswith("Line items: ")]
    assert len(chunks) == 1 and all(i["description"] in chunks[0] for i in inv["line_items"])


def test_correction_keeps_the_page_text_searchable(client, files):
    path, inv = files["digital"]
    _, ids = upload(client, path, name="corrected.pdf")
    rec = wait_done(client, ids[0])["invoices"][0]
    page_chunks = {c.id for c in _chunks(ids[0]) if c.page is not None}
    assert page_chunks
    assert client.patch(f"/api/invoices/{rec['id']}", json={"customer_name": "Renamed Customer Ltd"}).status_code == 200
    after = _chunks(ids[0])
    assert page_chunks == {c.id for c in after if c.page is not None}          # regression: a correction used to delete them
    assert any("Renamed Customer Ltd" in c.text for c in after if c.invoice_id)


def test_documents_from_an_older_version_are_brought_up_to_date(client, files):
    path, _ = files["digital"]
    _, ids = upload(client, path, name="old_model.pdf")
    wait_done(client, ids[0])
    name = embedder().name
    with SessionLocal() as s:                                                  # as an older version left them: another model, no line-item chunk
        for c in s.scalars(select(Chunk).where(Chunk.document_id == ids[0])):
            if c.text.startswith("Line items: "):
                s.delete(c)
            else:
                c.emb_model = "some-older-model"
                c.embedding = np.zeros(4, dtype=np.float32).tobytes()
        s.commit()
        assert ids[0] not in {h["document_id"] for h in search(s, "old_model invoice", "semantic", limit=50, min_score=0.0)}
    assert reindex_stale_chunks() >= 1
    assert add_missing_line_item_chunks() >= 1 and add_missing_line_item_chunks() == 0
    with SessionLocal() as s:
        assert {c.emb_model for c in s.scalars(select(Chunk).where(Chunk.document_id == ids[0]))} == {name}
        assert ids[0] in {h["document_id"] for h in search(s, "invoice", "semantic", limit=500, min_score=0.0)}
    assert sum(c.text.startswith("Line items: ") for c in _chunks(ids[0])) == 1


def test_old_dedupe_keys_are_brought_up_to_date(client):
    with SessionLocal() as s:
        d = Document(filename="old_key.pdf", stored_path="/x", file_type="pdf", size_bytes=1, sha256="x", status="completed")
        s.add(d); s.flush()
        inv = Invoice(document_id=d.id, invoice_number="SI/2025/7", dedupe_key="si20257", confidence={}, extraction_notes=[], line_items=[])
        s.add(inv); s.flush()
        assert refresh_dedupe_keys(s) >= 1 and inv.dedupe_key == dedupe_key("S1/2025/7")
        s.rollback()


def test_health_says_which_search_engine_is_running(client):
    h = client.get("/api/health").json()
    assert h["semantic_search"] is False and h["search_embedder"] in ("hash-char-ngram-768", "loading")      # the test config forces the fallback


def test_real_model_matches_meaning_without_shared_words():
    try:
        e = Embedder("fastembed")
    except Exception as err:
        pytest.skip(f"semantic model not available here: {err}")
    q, chair, toner = e.encode(["furniture for staff", "Line items: Office chair; Whiteboard stand", "Line items: Printer toner; Copper wire spool"])
    assert e.semantic and abs(np.linalg.norm(q) - 1) < 1e-3
    assert q @ chair > e.min_score and q @ chair > q @ toner


def test_semantic_results_stop_well_below_the_best_match(client, files, monkeypatch):
    path, _ = files["digital"]
    _, ids = upload(client, path, name="window.pdf")
    wait_done(client, ids[0])
    monkeypatch.setattr(embedder(), "window", 0.05)
    with SessionLocal() as s:
        everything = search(s, "invoice from supplier", "semantic", limit=100, min_score=0.0)
        interactive = search(s, "invoice from supplier", "semantic", limit=100)
    top = max(h["score"] for h in interactive)
    assert all(h["score"] >= top - 0.05 for h in interactive) and len(interactive) <= len(everything)
