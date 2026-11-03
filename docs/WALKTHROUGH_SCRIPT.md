# Walkthrough recording script (~6 min)
*A video cannot be produced by the assistant — record this yourself (Loom/OBS) following the script.*

1. **0:00 Architecture** — open `docs/architecture.md`; explain upload → queue → read → extract → ground → validate → index.
2. **0:45 Start** — `docker compose up` (or the local steps); show `/api/health` and `/docs` (Swagger).
3. **1:15 Upload** — first run `python -m evaluation.synth evaluation/data`, then drag in files from it: a digital PDF, a scanned PDF, a JPG, `register_0.xlsx`, a CSV and a corrupt file together. Show per-file progress and the rejection message for a `.txt`.
4. **2:30 Inspect** — open a scanned invoice: OCR confidence, fields with confidence, source text panel, history timeline.
5. **3:15 Validation** — open `badtotal_*.pdf` (total mismatch), `nodate_*.pdf` (missing date left empty, not invented), `dup_*.pdf` (duplicate) and `xsource_mismatch.xlsx` (PDF ↔ Excel mismatch).
6. **4:15 Correct** — fix the total, save → issues clear, status becomes Completed; approve another.
7. **4:45 Failure & retry** — corrupt file shows a clear error; click Reprocess / "Retry all failed".
8. **5:15 Search** — keyword by supplier, a filter on total range, then a semantic phrase.
9. **5:45 Evidence** — show `docs/EVALUATION_REPORT.md`, `docs/TEST_RESULTS.txt` (54 passed) and be upfront about the limitations section.
