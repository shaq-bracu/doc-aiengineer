"""Turn an uploaded file into pages of text (PDF/images) or sheets (spreadsheets)."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:  # older versions
    import fitz
import numpy as np
import pandas as pd
from PIL import Image, ImageSequence

from ..config import settings
from .ocr import OCRUnavailable, ocr_image


class UnsupportedFileError(ValueError):
    pass


class CorruptFileError(RuntimeError):
    pass


@dataclass
class Page:
    number: int
    text: str
    method: str            # text | ocr | failed
    ocr_conf: Optional[float] = None
    error: Optional[str] = None


@dataclass
class Sheet:
    name: str
    df: pd.DataFrame
    text: str = ""


_EXT = {"pdf": "pdf", "png": "image", "jpg": "image", "jpeg": "image", "tif": "image", "tiff": "image",
        "xlsx": "xlsx", "xls": "xls", "csv": "csv"}


def sniff_type(head: bytes, filename: str) -> str:
    """Validate extension AND magic bytes (never trust the client-supplied name/MIME)."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in _EXT:
        raise UnsupportedFileError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(_EXT))}")
    kind = _EXT[ext]
    ok = {
        "pdf": b"%PDF-" in head[:1024],
        "image": head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")),
        "xlsx": head.startswith(b"PK\x03\x04"),
        "xls": head.startswith(b"\xd0\xcf\x11\xe0"),
        "csv": b"\x00" not in head,
    }[kind]
    if not ok:
        raise UnsupportedFileError(f"File content does not match its '.{ext}' extension")
    return kind


# ------------------------------------------------------------------ PDF
def layout_text(page) -> str:
    """Rebuild text lines from word boxes so table rows stay on one line (cells separated by 3 spaces)."""
    words = page.get_text("words")
    if not words:
        return ""
    words.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows, cur, y0 = [], [], None
    for w in words:
        yc = (w[1] + w[3]) / 2
        if cur and abs(yc - y0) > max(3.0, (w[3] - w[1]) * 0.5):
            rows.append(cur)
            cur = []
        if not cur:
            y0 = yc
        cur.append(w)
    rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda w: w[0])
        s = r[0][4]
        for a, b in zip(r, r[1:]):
            s += ("   " if b[0] - a[2] > 12 else " ") + b[4]
        out.append(s)
    return "\n".join(out)


def _render(page) -> np.ndarray:
    pix = page.get_pixmap(dpi=settings.OCR_DPI, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return arr[:, :, 0].copy() if pix.n == 1 else arr[:, :, :3].copy()


def _ocr_page(args) -> Page:
    num, img = args
    try:
        text, conf = ocr_image(img)
        return Page(num, text, "ocr", conf)
    except OCRUnavailable as e:
        return Page(num, "", "failed", error=str(e))
    except Exception as e:  # one bad page must not kill a 1000-page job
        return Page(num, "", "failed", error=f"{type(e).__name__}: {e}")


def read_pdf(path, on_progress: Callable[[int, int], None] | None = None, batch: int = 8) -> list[Page]:
    try:
        doc = fitz.open(path)
    except Exception as e:
        # PyMuPDF echoes the file path; don't leak server storage paths to API clients
        raise CorruptFileError(f"Cannot open PDF: {str(e).replace(str(path), Path(path).name)}") from e
    try:
        if doc.needs_pass:
            raise CorruptFileError("PDF is password protected")
        n = doc.page_count
        if n == 0:
            raise CorruptFileError("PDF has no pages")
        pages: list[Page] = []
        with ThreadPoolExecutor(max_workers=settings.OCR_THREADS) as pool:
            for start in range(0, n, batch):  # bounded memory: render <= batch page images at a time
                results: dict[int, Page] = {}
                todo = []
                for i in range(start, min(n, start + batch)):
                    try:
                        page = doc.load_page(i)
                        text = layout_text(page)
                        if len(text.strip()) >= settings.MIN_TEXT_CHARS:
                            results[i] = Page(i + 1, text, "text")
                        else:
                            todo.append((i + 1, _render(page)))
                    except Exception as e:
                        results[i] = Page(i + 1, "", "failed", error=f"{type(e).__name__}: {e}")
                for p in pool.map(_ocr_page, todo):
                    results[p.number - 1] = p
                pages.extend(results[k] for k in sorted(results))
                if on_progress:
                    on_progress(min(n, start + batch), n)
        return pages
    finally:
        doc.close()


# ------------------------------------------------------------------ images
def read_image(path, on_progress=None) -> list[Page]:
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception as e:
        raise CorruptFileError(f"Cannot decode image: {e}") from e
    pages = []
    try:
        im = Image.open(path)
        frames = [f.copy() for f in ImageSequence.Iterator(im)]  # .copy(): the iterator re-uses one object
    except Exception as e:
        raise CorruptFileError(f"Cannot decode image: {e}") from e
    for i, fr in enumerate(frames):
        try:
            pages.append(_ocr_page((i + 1, np.array(fr.convert("RGB")))))
        except Exception as e:
            pages.append(Page(i + 1, "", "failed", error=str(e)))
        if on_progress:
            on_progress(i + 1, len(frames))
    return pages


# ------------------------------------------------------------------ spreadsheets
def _sheet_text(name: str, df: pd.DataFrame, max_rows: int = 20000) -> str:
    lines = [f"## Sheet: {name}"]
    for row in df.head(max_rows).itertuples(index=False):
        cells = [str(c).strip() for c in row if pd.notna(c) and str(c).strip()]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def read_spreadsheet(path, kind: str) -> list[Sheet]:
    try:
        if kind == "csv":
            frames = None
            for enc in ("utf-8-sig", "utf-16", "latin-1"):
                try:
                    frames = {"CSV": pd.read_csv(path, header=None, dtype=object, sep=None, engine="python",
                                                 encoding=enc, skip_blank_lines=True)}
                    break
                except (UnicodeError, pd.errors.ParserError):
                    continue
            if frames is None:
                raise CorruptFileError("CSV could not be decoded or parsed")
        else:
            frames = pd.read_excel(path, sheet_name=None, header=None, dtype=object,
                                   engine="openpyxl" if kind == "xlsx" else "xlrd")
    except CorruptFileError:
        raise
    except Exception as e:
        raise CorruptFileError(f"Cannot read {kind.upper()} file: {type(e).__name__}: {e}") from e
    sheets = []
    for name, df in frames.items():
        df = df.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
        if not df.empty:
            sheets.append(Sheet(str(name), df, _sheet_text(str(name), df)))
    if not sheets:
        raise CorruptFileError("Spreadsheet contains no data")
    return sheets
