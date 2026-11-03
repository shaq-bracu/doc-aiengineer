import pytest
from app.config import settings
from app.processing.ocr import ocr_available
from tests.conftest import upload, wait_done


def test_health(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and "ocr_available" in h


def test_upload_digital_pdf_end_to_end(client, files):
    path, inv = files["digital"]
    r, ids = upload(client, path)
    assert r.status_code == 201 and len(ids) == 1
    d = wait_done(client, ids[0])
    assert d["status"] == "completed" and d["progress"] == 1.0 and d["file_type"] == "pdf"
    rec = d["invoices"][0]
    for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"):
        assert rec[k] == inv[k], k
    assert len(rec["line_items"]) == len(inv["line_items"]) and rec["review_status"] == "ok"
    assert [e["stage"] for e in d["events"]][:2] == ["upload", "start"] and {"reading", "extracting", "validating", "indexing"} <= {e["stage"] for e in d["events"]}
    assert client.get(f"/api/documents/{ids[0]}/text").json()["text"].count(inv["invoice_number"]) >= 1


def test_multiple_files_in_one_request_and_rejections(client, files, tmp_path):
    exe = tmp_path / "evil.pdf"; exe.write_bytes(b"MZ\x90\x00not a pdf")
    txt = tmp_path / "notes.txt"; txt.write_text("hi")
    empty = tmp_path / "empty.csv"; empty.write_bytes(b"")
    r, ids = upload(client, files["xlsx"][0], files["csv"][0], exe, txt, empty)
    res = r.json()["results"]
    assert [x["accepted"] for x in res] == [True, True, False, False, False] and len(ids) == 2
    assert "extension" in res[2]["error"] and "Unsupported" in res[3]["error"] and "empty" in res[4]["error"].lower()
    assert all(wait_done(client, i)["status"] in ("completed", "needs_review") for i in ids)
    assert client.post("/api/documents", files=[("files", ("x.txt", b"hi"))]).status_code == 400


def test_spreadsheet_records_and_multi_invoice_pdf(client, files):
    _, ids = upload(client, files["xlsx"][0])
    d = wait_done(client, ids[0])
    assert d["invoice_count"] == 3 and all(i["source_ref"].startswith("Invoices!row") for i in d["invoices"])
    _, ids = upload(client, files["batch"][0])
    d = wait_done(client, ids[0])
    assert d["invoice_count"] == 3 and [i["page_start"] for i in d["invoices"]] == [1, 2, 3]


@pytest.mark.skipif(not ocr_available(), reason="tesseract not installed")
def test_scanned_pdf_and_poor_image(client, files):
    for key in ("scanned", "poor"):
        path, inv = files[key]
        _, ids = upload(client, path)
        d = wait_done(client, ids[0])
        assert d["status"] in ("completed", "needs_review") and d["ocr_pages"] >= 1
        assert d["invoices"][0]["total"] in (inv["total"], None)       # right value or null - never a wrong invented one


def test_validation_flags_wrong_total_and_missing_date(client, files):
    _, ids = upload(client, files["bad"][0], files["nodate"][0])
    bad, nod = (wait_done(client, i) for i in ids)
    rec = bad["invoices"][0]
    assert bad["status"] == "needs_review" and rec["review_status"] == "needs_review"
    assert "total_mismatch" in {i["code"] for i in rec["issues"]}
    assert nod["invoices"][0]["invoice_date"] is None
    assert ("missing_field", "invoice_date") in {(i["code"], i["field"]) for i in nod["invoices"][0]["issues"]}


def test_manual_correction_revalidates_and_clears_review(client, files):
    path, inv = files["bad"]
    _, ids = upload(client, path, name="bad_again.pdf")
    d = wait_done(client, ids[0])
    rec = d["invoices"][0]
    assert any(i["code"] in ("total_mismatch", "duplicate_invoice") for i in rec["issues"])
    fixed = client.patch(f"/api/invoices/{rec['id']}", json={"total": round(inv["subtotal"] + inv["tax"], 2), "invoice_number": inv["invoice_number"] + "-R"})
    assert fixed.status_code == 200
    body = fixed.json()
    assert body["edited"] and body["confidence"]["total"] == 1.0
    assert "total_mismatch" not in {i["code"] for i in body["issues"]} and body["review_status"] == "ok"
    assert client.get(f"/api/documents/{ids[0]}").json()["status"] == "completed"
    assert client.patch(f"/api/invoices/{rec['id']}", json={"invoice_date": "31/02/2025"}).status_code == 422
    assert client.patch(f"/api/invoices/{rec['id']}", json={"currency": "dollars"}).status_code == 422
    assert client.patch(f"/api/invoices/{rec['id']}", json={"bogus": 1}).status_code == 422


def test_approve_and_resolve(client, files):
    path, _ = files["nodate"]
    _, ids = upload(client, path, name="nodate2.pdf")
    rec = wait_done(client, ids[0])["invoices"][0]
    a = client.post(f"/api/invoices/{rec['id']}/approve").json()
    assert a["review_status"] == "approved" and all(i["resolved"] for i in a["issues"])


def test_search_keyword_filters_and_semantic(client, files):
    path, inv = files["digital"]
    _, ids = upload(client, path, name="searchable.pdf")
    wait_done(client, ids[0])
    kw = client.get("/api/search", params={"q": inv["supplier_name"].split()[0], "mode": "keyword"}).json()
    assert any(i["document"]["filename"] == "searchable.pdf" for i in kw["items"])
    f = client.get("/api/search", params={"mode": "keyword", "supplier": inv["supplier_name"], "min_total": inv["total"] - 1, "max_total": inv["total"] + 1, "currency": inv["currency"]}).json()
    assert f["count"] >= 1 and all(abs(i["invoices"][0]["total"] - inv["total"]) <= 1 for i in f["items"])
    assert client.get("/api/search", params={"q": "zzzzqqqq-no-such-thing", "mode": "keyword"}).json()["count"] == 0
    sem = client.get("/api/search", params={"q": f"invoice from {inv['supplier_name']} to {inv['customer_name']}", "mode": "semantic"}).json()
    assert sem["count"] >= 1 and sem["items"][0]["snippet"]
    assert client.get("/api/search", params={"q": "100%_; DROP TABLE documents;--", "mode": "keyword"}).status_code == 200   # injection-safe
    assert client.get("/api/search", params={"q": "x", "mode": "bogus"}).status_code == 422


def test_corrupt_file_fails_gracefully_and_reprocess(client, files):
    _, ids = upload(client, files["broken_pdf"], files["broken_png"])
    for i in ids:
        d = wait_done(client, i)
        assert d["status"] == "failed" and d["error"].startswith("corrupt_or_unreadable") and d["attempts"] == 1
    assert client.post(f"/api/documents/{ids[0]}/reprocess").status_code == 202
    assert wait_done(client, ids[0])["attempts"] == 2
    rq = client.post("/api/documents/reprocess-failed").json()["requeued"]
    assert set(ids) <= set(rq)
    for i in ids:
        wait_done(client, i)
    assert client.post("/api/documents/999999/reprocess").status_code == 404


def test_failed_job_can_be_reprocessed_to_success(client, files, tmp_path):
    p = tmp_path / "late.pdf"; p.write_bytes(files["broken_pdf"].read_bytes())
    _, ids = upload(client, p)
    assert wait_done(client, ids[0])["status"] == "failed"
    from app.db import session_scope
    from app.models import Document
    with session_scope() as s:                                  # simulate the file being repaired / re-delivered
        stored = s.get(Document, ids[0]).stored_path
    open(stored, "wb").write(files["digital"][0].read_bytes())
    client.post(f"/api/documents/{ids[0]}/reprocess")
    d = wait_done(client, ids[0])
    assert d["status"] in ("completed", "needs_review") and d["error"] is None and d["invoice_count"] == 1


def test_metrics_listing_and_delete(client, files):
    m = client.get("/api/metrics").json()
    assert m["documents_total"] >= 5 and 0 <= m["success_rate"] <= 1 and m["failure_rate"] > 0 and "total" in m["field_fill_rate"]
    lst = client.get("/api/documents", params={"status": "failed", "limit": 5}).json()
    assert lst["total"] >= 1 and all(d["status"] == "failed" for d in lst["items"])
    assert client.get("/api/invoices", params={"review_status": "needs_review"}).status_code == 200
    _, ids = upload(client, files["csv"][0], name="to_delete.csv"); wait_done(client, ids[0])
    assert client.delete(f"/api/documents/{ids[0]}").status_code == 204 and client.get(f"/api/documents/{ids[0]}").status_code == 404


def test_api_key_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "s3cret")
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/health").status_code == 200          # health stays open for load balancers
