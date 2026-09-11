"""Detect image-only PDFs and recover a text layer with OCR.

Phase 3 told reviewers that scanned PDFs must be OCR'd before upload. That put
the burden on the person least equipped to do it. Phase 4 detects the condition
and, when OCR is enabled, produces the text layer itself.

Two honesty rules are enforced in the data, not just the documentation:

* OCR output is marked as OCR everywhere it appears, with the engine and
  confidence, because a quotation "located" in OCR text is weaker evidence than
  one located in an embedded text layer.
* Page character offsets refer to the OCR text for OCR'd pages. They are not
  presented as offsets into the publisher's original text.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from src.evidence.text import digest_text

logger = logging.getLogger(__name__)

OCR_METHOD_VERSION = "ocr-v1"


@dataclass
class PageAssessment:
    page: int
    characters: int
    needs_ocr: bool


@dataclass
class TextLayerAssessment:
    """Whether a PDF carries a usable embedded text layer."""

    pages: list[PageAssessment] = field(default_factory=list)
    status: str = "unknown"  # text | needs_ocr | mixed | empty
    text_page_ratio: float = 0.0
    total_characters: int = 0

    @property
    def needs_ocr(self) -> bool:
        return self.status in {"needs_ocr", "mixed", "empty"}

    def pages_needing_ocr(self) -> list[int]:
        return [page.page for page in self.pages if page.needs_ocr]

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "page_count": len(self.pages),
            "text_page_ratio": round(self.text_page_ratio, 4),
            "total_characters": self.total_characters,
            "pages_needing_ocr": self.pages_needing_ocr(),
        }


def assess_text_layer(source: dict, *, min_chars: int = 120, min_ratio: float = 0.6) -> TextLayerAssessment:
    """Classify a `pdf_source()` result by how much extractable text it has."""
    pages = [
        PageAssessment(
            page=int(page.get("page", index + 1)),
            characters=len((page.get("text") or "").strip()),
            needs_ocr=len((page.get("text") or "").strip()) < min_chars,
        )
        for index, page in enumerate(source.get("pages", []))
    ]
    total = sum(page.characters for page in pages)
    if not pages:
        return TextLayerAssessment(pages, "empty", 0.0, 0)
    with_text = sum(1 for page in pages if not page.needs_ocr)
    ratio = with_text / len(pages)
    if total == 0:
        status = "empty"
    elif ratio >= min_ratio and with_text == len(pages):
        status = "text"
    elif ratio >= min_ratio:
        status = "mixed"
    else:
        status = "needs_ocr"
    return TextLayerAssessment(pages, status, ratio, total)


def tesseract_available() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


class OcrUnavailable(RuntimeError):
    """OCR was requested but the engine or its dependencies are absent."""


def _render_page(path: Path, page_number: int, dpi: int):
    """Rasterize one page with pypdfium2, which is already a project dependency."""
    try:
        import pypdfium2
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise OcrUnavailable("install pypdfium2 to rasterize pages for OCR") from exc
    document = pypdfium2.PdfDocument(str(path))
    try:
        page = document[page_number - 1]
        bitmap = page.render(scale=max(0.5, dpi / 72.0))
        return bitmap.to_pil()
    finally:
        document.close()


def ocr_page(path: Path, page_number: int, *, dpi: int = 300, languages: str = "eng") -> dict:
    """OCR a single page and report its mean word confidence."""
    if not tesseract_available():
        raise OcrUnavailable(
            "the tesseract binary and pytesseract are required for OCR; "
            "install them or set CITEPRISM_ENABLE_OCR=false"
        )
    import pytesseract

    image = _render_page(path, page_number, dpi)
    text = pytesseract.image_to_string(image, lang=languages) or ""
    confidence = None
    try:
        data = pytesseract.image_to_data(
            image, lang=languages, output_type=pytesseract.Output.DICT
        )
        scores = [
            float(value)
            for value in data.get("conf", [])
            if str(value) not in {"-1", ""} and str(value).replace(".", "", 1).isdigit()
        ]
        if scores:
            confidence = round(sum(scores) / len(scores) / 100.0, 4)
    except Exception:  # noqa: BLE001 - confidence is informative, not required
        logger.debug("Per-word OCR confidence was unavailable for page %s.", page_number)
    return {
        "page": page_number,
        "text": text,
        "text_sha256": digest_text(text),
        "characters": len(text.strip()),
        "confidence": confidence,
    }


def apply_ocr(
    path: str | Path,
    source: dict,
    assessment: TextLayerAssessment,
    *,
    dpi: int = 300,
    languages: str = "eng",
    max_pages: int = 60,
    progress=None,
) -> dict:
    """Return a new source document with OCR text merged into the weak pages.

    The original extraction result is not mutated. Pages that already had text
    keep it, and every replaced page is labeled so downstream grounding can tell
    the difference.
    """
    path = Path(path)
    targets = assessment.pages_needing_ocr()[: max(1, max_pages)]
    if not targets:
        return source

    pages = [dict(page) for page in source.get("pages", [])]
    by_number = {int(page.get("page", index + 1)): page for index, page in enumerate(pages)}
    applied, failures, confidences = [], [], []

    for position, page_number in enumerate(targets, 1):
        if progress is not None:
            try:
                progress(f"Reading page {page_number} with OCR ({position}/{len(targets)})")
            except Exception:  # noqa: BLE001 - progress display is never critical
                pass
        try:
            result = ocr_page(path, page_number, dpi=dpi, languages=languages)
        except OcrUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - one bad page must not stop the rest
            logger.warning("OCR failed on page %s: %s", page_number, type(error).__name__)
            failures.append({"page": page_number, "error": type(error).__name__})
            continue
        page = by_number.get(page_number)
        if page is None or not result["characters"]:
            continue
        page["text"] = result["text"]
        page["text_sha256"] = result["text_sha256"]
        page["text_source"] = "ocr"
        page["ocr_confidence"] = result["confidence"]
        applied.append(page_number)
        if result["confidence"] is not None:
            confidences.append(result["confidence"])

    for page in pages:
        page.setdefault("text_source", "embedded")

    result = dict(source)
    result["pages"] = pages
    result["ocr"] = {
        "method_version": OCR_METHOD_VERSION,
        "engine": "tesseract",
        "languages": languages,
        "dpi": dpi,
        "pages_requested": targets,
        "pages_applied": applied,
        "pages_failed": failures,
        "mean_confidence": round(sum(confidences) / len(confidences), 4)
        if confidences
        else None,
        "assessment_before": assessment.as_dict(),
        "note": (
            "Character offsets on OCR pages refer to recognized text, not to the "
            "publisher's original text layer. Quotations located in OCR output "
            "carry recognition error and need closer human checking."
        ),
    }
    return result


def prepare_source(path: str | Path, source: dict, config, *, progress=None) -> tuple[dict, dict]:
    """Assess a document and OCR it when configuration and content both call for it.

    Returns the (possibly OCR-augmented) source and a status dictionary suitable
    for storage on the manuscript record.
    """
    assessment = assess_text_layer(
        source,
        min_chars=int(getattr(config, "OCR_MIN_CHARS_PER_PAGE", 120)),
        min_ratio=float(getattr(config, "OCR_MIN_TEXT_PAGE_RATIO", 0.6)),
    )
    status = {"text_layer": assessment.as_dict(), "ocr_applied": False, "text_layer_status": "text"}

    if not assessment.needs_ocr:
        return source, status

    if not getattr(config, "ENABLE_OCR", False) or getattr(config, "OCR_ENGINE", "none") == "none":
        status["text_layer_status"] = "needs_ocr"
        status["reason"] = (
            "This document has little or no extractable text. Enable OCR "
            "(CITEPRISM_ENABLE_OCR=true) or upload a text-based PDF."
        )
        return source, status

    try:
        augmented = apply_ocr(
            path,
            source,
            assessment,
            dpi=int(getattr(config, "OCR_DPI", 300)),
            languages=str(getattr(config, "OCR_LANGUAGES", "eng")),
            max_pages=int(getattr(config, "OCR_MAX_PAGES", 60)),
            progress=progress,
        )
    except OcrUnavailable as error:
        status["text_layer_status"] = "ocr_failed"
        status["reason"] = str(error)
        return source, status

    report = augmented.get("ocr") or {}
    status["ocr_applied"] = bool(report.get("pages_applied"))
    status["text_layer_status"] = "ocr_applied" if status["ocr_applied"] else "ocr_failed"
    status["ocr"] = report
    if not status["ocr_applied"]:
        failed = report.get("pages_failed") or []
        status["reason"] = (
            "OCR ran but recovered no usable text"
            + (f" ({len(failed)} page(s) could not be read)." if failed else ".")
            + " Check that the file is a readable PDF and that the OCR language "
            "pack matches the document."
        )
    return augmented, status


__all__ = [
    "OCR_METHOD_VERSION",
    "OcrUnavailable",
    "PageAssessment",
    "TextLayerAssessment",
    "apply_ocr",
    "assess_text_layer",
    "ocr_page",
    "prepare_source",
    "tesseract_available",
]
