"""Central configuration (12-factor: everything comes from environment variables)."""
import os
from pathlib import Path


def _int(name, default):
    return int(os.getenv(name, default))


class Settings:
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/docintel.db")
    UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./data/uploads"))
    MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 200)
    MAX_FILES_PER_REQUEST = _int("MAX_FILES_PER_REQUEST", 100)
    API_KEY = os.getenv("API_KEY", "")  # empty => auth disabled (local dev)
    CORS_ORIGINS = [o for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",") if o]

    # --- processing ---
    WORKERS = _int("WORKERS", 2)                 # concurrent documents
    OCR_THREADS = _int("OCR_THREADS", 4)         # concurrent pages inside one document
    OCR_LANGS = os.getenv("OCR_LANGS", "eng")    # e.g. "eng+ben" for Bangla (needs tesseract-ocr-ben)
    OCR_DPI = _int("OCR_DPI", 200)
    OCR_PSM = os.getenv("OCR_PSM", "6")
    MIN_TEXT_CHARS = _int("MIN_TEXT_CHARS", 30)  # below this a PDF page is treated as scanned

    # --- extraction ---
    EXTRACTION_MODE = os.getenv("EXTRACTION_MODE", "auto")  # auto | llm | rules
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
    DATE_DAYFIRST = os.getenv("DATE_DAYFIRST", "true").lower() == "true"

    # --- validation ---
    AMOUNT_TOLERANCE = float(os.getenv("AMOUNT_TOLERANCE", "0.02"))
    REVIEW_CONFIDENCE_THRESHOLD = float(os.getenv("REVIEW_CONFIDENCE_THRESHOLD", "0.6"))

    # --- search ---
    EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "auto")  # auto | st | hash
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")


settings = Settings()
