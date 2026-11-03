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
* Added an `.xls` (BIFF) test, then re-ran the 53 tests and the evaluation inside the backend image (Tesseract 5.5) and refreshed `EVALUATION_REPORT.md` / `TEST_RESULTS.txt`.

## Runtime prompt (sent to the LLM when `ANTHROPIC_API_KEY` is set)
Source of truth: `backend/app/extraction/llm.py` (`SYSTEM`). The user turn is the invoice text wrapped in `<document>…</document>`. Key properties: temperature 0, JSON-only output, "never guess / return null", per-field confidence, and an explicit instruction to ignore instructions embedded in the document (prompt-injection defence). The response is then schema-normalised and grounded against the source text in `extractor.py`.
