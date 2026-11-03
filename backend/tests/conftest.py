import os
import random
import tempfile
import time
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="docintel_test_")
os.environ.update(DATABASE_URL=f"sqlite:///{_tmp}/test.db", UPLOAD_DIR=f"{_tmp}/uploads", WORKERS="2", EXTRACTION_MODE="rules",
                  ANTHROPIC_API_KEY="", API_KEY="", EMBEDDING_BACKEND="hash")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from evaluation import synth  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def files(tmp_path_factory):
    """A handful of generated files (digital/scanned/image/spreadsheet/corrupt)."""
    d = tmp_path_factory.mktemp("files")
    rng = random.Random(1234)
    f = {}
    inv = synth.make_invoice(rng, 1); synth.render_pdf(d / "digital.pdf", [inv]); f["digital"] = (d / "digital.pdf", inv)
    inv = synth.make_invoice(rng, 2); synth.render_pdf(d / "t.pdf", [inv])
    imgs = [synth.degrade(i, rng, "good") for i in synth.pdf_to_images(d / "t.pdf", 200)]
    synth.save_image_pdf(imgs, d / "scanned.pdf"); f["scanned"] = (d / "scanned.pdf", inv)
    inv = synth.make_invoice(rng, 3); synth.render_pdf(d / "t3.pdf", [inv])
    synth.degrade(synth.pdf_to_images(d / "t3.pdf", 150)[0], rng, "poor").save(d / "poor.jpg", quality=55); f["poor"] = (d / "poor.jpg", inv)
    invs = [synth.make_invoice(rng, k + 1) for k in range(3)]; synth.render_pdf(d / "batch.pdf", invs); f["batch"] = (d / "batch.pdf", invs)
    invs = [synth.make_invoice(rng, 1) for _ in range(3)]; synth.write_xlsx(d / "reg.xlsx", invs); f["xlsx"] = (d / "reg.xlsx", invs)
    invs = [synth.make_invoice(rng, 2) for _ in range(3)]; synth.write_csv(d / "reg.csv", invs); f["csv"] = (d / "reg.csv", invs)
    inv = synth.make_invoice(rng, 1, "bad_total"); synth.render_pdf(d / "bad.pdf", [inv]); f["bad"] = (d / "bad.pdf", inv)
    inv = synth.make_invoice(rng, 3, "no_date"); synth.render_pdf(d / "nodate.pdf", [inv]); f["nodate"] = (d / "nodate.pdf", inv)
    (d / "broken.pdf").write_bytes(b"%PDF-1.7\n" + bytes(rng.randrange(256) for _ in range(2000)))
    (d / "broken.png").write_bytes((d / "poor.jpg").read_bytes()[:50].replace(b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", 1))
    f["broken_pdf"], f["broken_png"] = d / "broken.pdf", d / "broken.png"
    return f


def wait_done(client, doc_id, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get(f"/api/documents/{doc_id}").json()
        if d["status"] not in ("queued", "processing"):
            return d
        time.sleep(0.3)
    raise TimeoutError(f"document {doc_id} still processing")


def upload(client, *paths, name=None):
    r = client.post("/api/documents", files=[("files", (name or Path(p).name, open(p, "rb"))) for p in paths])
    return r, [x["document"]["id"] for x in r.json()["results"] if x["accepted"]]
