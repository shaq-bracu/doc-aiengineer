"""OCR with OpenCV pre-processing (upscale -> denoise -> deskew -> optional binarise)."""
import os
import shutil

import cv2
import numpy as np
import pytesseract

from ..config import settings

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # we parallelise across pages instead


class OCRUnavailable(RuntimeError):
    pass


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Projection-profile deskew: pick the rotation (-6..6 deg) that makes text rows sharpest."""
    h, w = gray.shape
    scale = min(1.0, 800 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    sh, sw = bw.shape
    best, best_score = 0.0, -1.0
    for a in np.arange(-6.0, 6.01, 0.5):
        m = cv2.getRotationMatrix2D((sw / 2, sh / 2), float(a), 1.0)
        score = float(np.var(cv2.warpAffine(bw, m, (sw, sh), flags=cv2.INTER_NEAREST).sum(axis=1)))
        if score > best_score:
            best, best_score = float(a), score
    if abs(best) < 0.4:
        return gray
    m = cv2.getRotationMatrix2D((w / 2, h / 2), best, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def preprocess(img: np.ndarray) -> np.ndarray:
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if max(gray.shape) < 1600:  # low-res input: upscale so glyphs are >= ~20px tall
        f = 1600 / max(gray.shape)
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    gray = cv2.medianBlur(gray, 3)
    return _deskew(gray)


def _binarize(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 15)


def _run(img: np.ndarray, lang: str):
    d = pytesseract.image_to_data(img, lang=lang, config=f"--psm {settings.OCR_PSM}",
                                  output_type=pytesseract.Output.DICT)
    lines, confs = {}, []
    for i, t in enumerate(d["text"]):
        t = t.strip()
        if not t:
            continue
        c = float(d["conf"][i])
        if c >= 0:
            confs.append(c)
        lines.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), []).append((d["left"][i], d["top"][i], t))
    ordered = sorted(lines.values(), key=lambda ws: min(w[1] for w in ws))
    text = "\n".join(" ".join(w[2] for w in sorted(ws)) for ws in ordered)
    return text, (sum(confs) / len(confs) if confs else 0.0)


def ocr_image(img: np.ndarray, lang: str | None = None) -> tuple[str, float]:
    """Return (text, mean_word_confidence 0-100). Tries a binarised variant when confidence is low."""
    if not ocr_available():
        raise OCRUnavailable("Tesseract binary not found; install tesseract-ocr to process scanned files")
    lang = lang or settings.OCR_LANGS
    gray = preprocess(img)
    best = _run(gray, lang)
    if best[1] < 70:
        alt = _run(_binarize(gray), lang)
        if alt[1] > best[1]:
            best = alt
    return best
