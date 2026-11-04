# Document Intelligence System

AI-powered pipeline that ingests invoices as **PDF, JPG, PNG, TIFF, XLSX, XLS, CSV**, extracts structured data (OCR for scans), validates it, and makes everything searchable through a REST API and a React UI.

| Assignment task | Where it lives |
|---|---|
| 1 Document processing | `backend/app/processing/` (PDF text layer, OCR + OpenCV, TIFF, spreadsheets, corrupt-file handling) |
| 2 AI extraction | `backend/app/extraction/` (Claude LLM + rule extractor + spreadsheet table parser + **grounding**) |
| 3 Validation | `backend/app/validation.py` |
| 4 Backend API + background jobs | `backend/app/main.py`, `backend/app/pipeline.py` |
| 5 Database + search | `backend/app/models.py`, `backend/app/search.py`, `docs/schema.sql` |
| 6 Frontend | `frontend/src/` (React + TypeScript + Vite) |
| 7 Tests + evaluation | `backend/tests/` (97 tests), `backend/evaluation/`, `docs/EVALUATION_REPORT.md` |

Architecture diagram: [`docs/architecture.md`](docs/architecture.md) · DB schema: [`docs/schema.sql`](docs/schema.sql) · AI disclosure + prompts: [`docs/AI_PROMPTS_AND_DISCLOSURE.md`](docs/AI_PROMPTS_AND_DISCLOSURE.md) · Walkthrough script: [`docs/WALKTHROUGH_SCRIPT.md`](docs/WALKTHROUGH_SCRIPT.md)

## Deliverables checklist
| # | Deliverable | Where | How it was verified |
|---|---|---|---|
| 1 | Source code in Git | this repository | CI (`.github/workflows/ci.yml`) runs tests, evaluation and the frontend build on every push |
| 2 | README + setup | this file | Docker and local (SQLite) paths both run |
| 3 | Architecture diagram + DB schema | `docs/architecture.md`, `docs/schema.sql` | `schema.sql` diffed against the live PostgreSQL schema: 66/66 columns match |
| 4 | Working backend API + frontend | `backend/app/`, `frontend/src/` | All 16 endpoints covered by API tests; in the browser, the UI's upload (with rejection), progress, detail/history, correction and search flows were exercised against the Docker stack |
| 5 | PDF, image, Excel, CSV pipelines | `backend/app/processing/` | Tests for digital/scanned/multi-page PDF, JPG/PNG/multi-page TIFF, XLSX, XLS, CSV and corrupt files |
| 6 | Automated tests + results | `backend/tests/`, `docs/TEST_RESULTS.txt` | 97/97 passed inside the backend image (Tesseract 5.5, the bundled search model) |
| 7 | Evaluation with measured metrics | `docs/EVALUATION_REPORT.md` | `python -m evaluation.run_eval` on a labelled 48-file / 75-invoice set, and `python -m evaluation.run_scale` on a 1,000-page scan |
| 8 | Recorded walkthrough | link: *to be added* | Script in `docs/WALKTHROUGH_SCRIPT.md` |
| 9 | AI prompts + disclosure | `docs/AI_PROMPTS_AND_DISCLOSURE.md` | — |

## Setup

### Option A: Docker (PostgreSQL, everything included)
```bash
cp .env.example .env            # optionally add ANTHROPIC_API_KEY
docker compose up --build
# UI: http://localhost:3000   API docs: http://localhost:8000/docs
```

### Option B: Local development (SQLite, no Docker)
Prerequisites: Python 3.10+, Node 18+, **Tesseract OCR** (`sudo apt install tesseract-ocr` · `brew install tesseract` · Windows: UB-Mannheim installer, add to PATH). For Bangla add `tesseract-ocr-ben` and set `OCR_LANGS=eng+ben`.
```bash
# backend
cd backend
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=sk-ant-...                       # optional; without it extraction is rules-only
uvicorn app.main:app --reload --port 8000

# frontend (new terminal)
cd frontend && npm install && npm run dev                 # http://localhost:5173 (proxies /api to :8000)
```
Semantic search runs `fastembed` (ONNX, no PyTorch) with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. The Docker image ships the model, so containers need no network. A local install downloads it (about 220 MB) on first use. If it cannot load, search falls back to text patterns, and `/api/health` and the System page say so.

### Tests and evaluation
```bash
cd backend
pytest -q                                   # 97 tests: parsing, OCR, spreadsheets, extraction, look-alikes, validation, search, API, failures
python -m evaluation.synth evaluation/data  # (optional) write the labelled sample files to disk to try in the UI
python -m evaluation.run_eval               # regenerates the dataset, measures everything, writes evaluation/results/EVAL_REPORT.md
python -m evaluation.run_eval --data DIR    # evaluate YOUR labelled files (DIR/labels.json, same format as synth.py output)
python -m evaluation.run_scale --api http://localhost:8000   # a 1,000-page scanned PDF through a running stack; run it before run_eval to include it in the report
```

## API (all under `/api`, interactive docs at `/docs`)
| Method & path | Purpose |
|---|---|
| `POST /documents` | Upload many files (multipart `files`). Returns per-file accepted/rejected; processing is asynchronous |
| `GET /documents?status=&q=&limit=&offset=` · `GET /documents/{id}` | List / status, progress, stage, invoices, issues, processing history |
| `GET /documents/{id}/text` · `/file` | Extracted text · original file |
| `POST /documents/{id}/reprocess` · `POST /documents/reprocess-failed` · `DELETE /documents/{id}` | Retry one / all failed jobs; delete |
| `GET /invoices?review_status=` · `GET /invoices/{id}` | Extracted results (with confidence + validation issues) |
| `PATCH /invoices/{id}` | Manual correction: re-validates, sets that field's confidence to 1.0, logs to history |
| `POST /invoices/{id}/approve` · `POST /issues/{id}/resolve` | Reviewer sign-off |
| `GET /search?q=&mode=keyword\|semantic\|hybrid&supplier=&date_from=&date_to=&min_total=&max_total=&currency=&status=` | Search + filters |
| `GET /metrics` · `GET /health` | Success/failure rate, field fill rates, queue depth · liveness |

Extracted JSON (per invoice): `invoice_number, invoice_date (ISO), supplier_name, customer_name, subtotal, tax, total, currency, line_items[{description, quantity, unit_price, amount}]` plus `confidence{}`, `uncertain_fields[]`, `notes[]`, `issues[]`. Missing or unverifiable values are `null`.

## How it works (the AI-engineering part)
* **No invented values.** The LLM is treated as an untrusted parser: JSON-only, temperature 0, "return null if unsure", and the document text is fenced as data (prompt-injection defence). Afterwards every returned value is *grounded*: numbers, dates, IDs and names must be found in the source text (fuzzy for OCR noise) or they are set to `null` and noted. Ambiguous `03/04/2025` dates and bare `$` currencies get low confidence instead of a guess.
* **Hybrid extraction.** Spreadsheets use a deterministic table parser (header detection, column aliases, line-item grouping). Text/OCR uses the LLM when `ANTHROPIC_API_KEY` is set, with the rule extractor filling confident gaps and acting as the fallback if the API fails, so a document is never lost to an outage.
* **OCR quality.** Pages are rendered at 200 DPI, upscaled if small and deskewed (projection profile). A median blur and adaptive thresholding are tried only when Tesseract's mean confidence is below 70. Measured on scanned invoice numbers, blurring every page cost accuracy and time, and both 300 DPI and Tesseract's `best` model misread `SI/` as `S1/` more often, not less. OCR confidence scales every field's confidence, so noisy scans land in the review queue.
* **Look-alike characters.** OCR confuses I with 1 and sometimes reads I as `!` or `|`. An OCR-read invoice number whose segments mix letters with look-alike digits, such as `S1/2025/00020`, is flagged for review with the reason. Inside one file it is repaired only when at least three clean readings from the same supplier, and a majority of them, share its exact format, serial length included; the repair is logged on the invoice. A real `S1` was never read as `SI` when this was measured, which is what makes clean readings trustworthy evidence. Look-alikes also fold together for duplicate and PDF-to-Excel checks, so a misread cannot hide a duplicate.
* **Search.** Keyword filters cover file names, extracted text, invoice numbers and parties. Vector search covers three kinds of chunk per invoice: a summary line, a line-items line and the page text, so a query like "furniture for staff" finds invoices for office chairs. Interactive results keep matches within 0.15 of the best score. At startup, older data is brought up to date: chunks from another model are re-embedded and missing line-item chunks are added (rehearsed on PostgreSQL by upgrading a stack from the previous image). A manual correction rebuilds only that invoice's chunks.
* **Scale (about 1000-page PDFs).** Pages stream in batches of 8 with parallel OCR (`OCR_THREADS`), progress is committed every second, one bad page is logged without failing the file, and a multi-invoice PDF is split into one record per invoice. Measured on a synthetic 1,000-page scan through the Docker stack: 2 min 12 s, all 1,000 invoices found, 99.9% of invoice numbers and totals right, and every wrong header field sent to review.
* **Validation rules:** missing required/optional fields · negative/zero/implausible/unparseable numbers · future or ancient dates · `subtotal + tax ≠ total` (tolerance 0.02) · `Σ line items ≠ subtotal` · `qty × unit price ≠ line amount` · **duplicate invoices** (same normalised number + compatible supplier) · **cross-source checks** (same invoice in PDF vs Excel/CSV: `cross_verified` if equal, `cross_source_mismatch` if values differ) · low-confidence fields · OCR look-alike characters in invoice numbers. Anything with an error or low confidence goes to `needs_review`.
* **Reliability.** The DB is the source of truth; unfinished jobs are re-queued on restart; every stage writes an audit event; failures store a categorised error and are retryable.
* **Security.** Extension *and* magic-byte checks, random storage names, size/file-count limits, optional `X-API-Key` (constant-time compare), parameterised queries (LIKE wildcards escaped), non-root container, security headers in nginx, CORS allow-list. Still needed for production: real user auth (JWT/OIDC), per-user data scoping, rate limiting, virus scanning, TLS at the proxy, and never shipping `VITE_API_KEY` in a browser bundle.

## Measured results
See [`docs/EVALUATION_REPORT.md`](docs/EVALUATION_REPORT.md) (generated by `evaluation/run_eval.py`) and [`docs/TEST_RESULTS.txt`](docs/TEST_RESULTS.txt). **Read the report's limitations section:** the labelled set is synthetic and was generated by the same author as the rules extractor, scans are simulated, and the run used rules-only extraction (no API key was configured); search used the bundled semantic model. Re-run the harness with your API key and on real labelled invoices before quoting accuracy to anyone.

## Known limitations / next steps
* The Docker/PostgreSQL stack was run end-to-end (all 48 sample files uploaded through nginx → 75 invoices in PostgreSQL, 44/44 valid files processed, 4/4 corrupt files failed cleanly, reprocess and manual correction verified). The full test-suite (97/97, incl. OCR and the search model) and the evaluation were run inside the backend image.
* The queue is in-process; for multi-node scale switch to Celery/RQ/BullMQ workers calling `pipeline.process_document`.
* Amounts are floats with tolerance; use `NUMERIC`/minor units for ledger-grade accuracy. `.xls` (xlrd) is covered by a test that writes a real BIFF workbook with xlwt.
* Embeddings are searched with numpy in memory; move to pgvector/Qdrant beyond about 10⁵ chunks.
* Two extraction errors in the labelled set are still not flagged: a line row whose quantity OCR read as `C4`, with no arithmetic to prove the split, and `SV/` read for `SI/` on the poorest photo, a letter-for-letter swap that no look-alike rule covers. Per-word OCR confidence was measured as a signal and rejected, because it is just as low on correct readings of these numbers.
* The backend image is about 2.1 GB, mostly OCR and the bundled search model.
* The rules extractor is label-driven (English). The LLM path (and `OCR_LANGS=eng+ben`) is the route to Bangla and unusual layouts; that path is unit-tested with a stub but not benchmarked here.
* Not implemented: user accounts, multi-currency conversion, PO matching, line-item-level cross-verification.
