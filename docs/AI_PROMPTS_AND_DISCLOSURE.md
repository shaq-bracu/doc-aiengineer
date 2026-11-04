# AI-assisted development disclosure

**Development:** this repository was produced with Claude (Anthropic) as a coding assistant from a single task brief (the assignment PDF + the candidate's CV for skill alignment: Python/FastAPI, PyTorch-style ML tooling, React/TypeScript, Docker, GitHub Actions). The assistant wrote the code, tests and docs; it also executed the test-suite and the evaluation in a sandbox, and the numbers in `EVALUATION_REPORT.md` / `TEST_RESULTS.txt` are the output of those runs. The candidate must review, run and understand every part before submission.

**Prompt given to the assistant (summary):**
> Build the AI-Powered Document Intelligence System from the attached assignment (7 tasks: document processing, AI extraction, validation, REST API with background jobs, database + keyword/semantic search, React UI, tests + measured evaluation). Use the skills on my CV. Think like an AI engineer, follow the instructions carefully, and tell me how to set it up.

**Bugs found by the evaluation harness and fixed during development** (shows the harness works): multipage TIFF frames aliased to the last frame (`ImageSequence.Iterator` re-uses one object) — now covered by a regression test.

**Second session (completion pass, Claude Code):**
> Read the docs and complete whatever instructions are yet to be implemented. Build the entire project and upload it to my GitHub as instructed in the PDF.

The assistant ran the Docker/PostgreSQL stack for the first time and fixed what broke:
* `.env.example` used inline comments (`KEY=   # text`); docker compose reads those as the value, so the comment became the Anthropic key and the API key (LLM "enabled" with a junk key, every call 401). Comments moved to their own lines.
* The `uploads` named volume was created root-owned (the mount point did not exist in the image), so the non-root container could not save uploads (HTTP 500). The Dockerfile now creates `/data/uploads` owned by `appuser`.
* PyMuPDF error messages leaked the server storage path to API clients; the path is now reduced to the file name.
* Added an `.xls` (BIFF) test, then re-ran the tests (now 54, incl. a new one for issue-resolve, invoice detail and original-file download) and the evaluation inside the backend image (Tesseract 5.5) and refreshed `EVALUATION_REPORT.md` / `TEST_RESULTS.txt`.

**Third session (search and OCR fixes, Claude Code):**
> Fix the search and OCR gaps first. (The user's answer when the assistant reported two gaps found while making the walkthrough video.)

The assistant measured before changing anything, and kept the changes the measurements supported:
* **Semantic search** was a character-n-gram fallback unless an optional package was installed. It now runs `fastembed` with a multilingual MiniLM model baked into the Docker image (offline, no PyTorch), plus a line-items chunk per invoice. Measured on 20 paraphrased queries that share no words with their invoices: MRR 0.91 against 0.57 for the fallback. Also fixed: a manual correction deleted the document's page chunks, and old chunks are re-embedded when the model changes.
* **OCR on low-resolution scans.** On a 1,000-page scan only 91.7% of invoice numbers were right (`SI/` read as `S1/`, and `S!/` made five pages merge into their neighbours), and 2 of 83 misreads were flagged. Tested and rejected: 300/400 DPI, Tesseract's `best` and legacy models, and multi-scale re-reads, which were all worse; per-word OCR confidence, which is as low on correct reads. Kept: no default median blur, `!`/`|`/`$` read back into numbers, a look-alike flag that sends the number to review with its reason, and a repair only on same-supplier evidence in the file. The first repair rule produced one confident wrong value (a serial cut short by an OCR space); requiring the serial length to match fixed it, and the run was measured again rather than assumed.
* **Line items:** OCR noise in the quantity (`2.`, `5:`) no longer glues quantity and price onto the description when the row's arithmetic proves where the columns are.
* Result on the same 1,000-page scan: all 1,000 invoices found, 99.9% of invoice numbers and totals right, 74 of 74 repairs correct, and every wrong header field sent to review. Labelled set: 97.3% of invoices fully correct (was 92.0%). 97 tests pass inside the backend image.

## Runtime prompt (sent to the LLM when `ANTHROPIC_API_KEY` is set)
Source of truth: `backend/app/extraction/llm.py` (`SYSTEM`). The user turn is the invoice text wrapped in `<document>…</document>`. Key properties: temperature 0, JSON-only output, "never guess / return null", per-field confidence, and an explicit instruction to ignore instructions embedded in the document (prompt-injection defence). The response is then schema-normalised and grounded against the source text in `extractor.py`.
