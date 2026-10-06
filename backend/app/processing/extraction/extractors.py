"""Format-specific extractors. Pure functions over a local file — they run inside the
extraction subprocess (see cli.py), never in the worker process itself."""

import zipfile
from pathlib import Path

import docx
import pymupdf
from charset_normalizer import from_path
from docx.table import Table
from docx.text.paragraph import Paragraph
from PIL import Image, ImageSequence, UnidentifiedImageError

from app.processing.extraction.ocr import ocr_image
from app.processing.extraction.types import (
    ExtractedPage,
    ExtractionFailed,
    ExtractionLimits,
    ExtractionResult,
    clean_text,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMAGE_MIMES = {"image/png", "image/jpeg", "image/tiff"}
MAX_RENDER_PIXELS = 40_000_000  # caps OCR rendering of huge pages


def extract(path: Path, mime_type: str, limits: ExtractionLimits) -> ExtractionResult:
    if mime_type == "application/pdf":
        return extract_pdf(path, limits)
    if mime_type == DOCX_MIME:
        return extract_docx(path, limits)
    if mime_type == "text/plain":
        return extract_text(path)
    if mime_type in IMAGE_MIMES:
        return extract_image(path, limits)
    raise ExtractionFailed("UNSUPPORTED_FILE_TYPE", f"No extractor for {mime_type}")


# --- PDF ---------------------------------------------------------------------------------------


def extract_pdf(path: Path, limits: ExtractionLimits) -> ExtractionResult:
    try:
        document = pymupdf.open(path, filetype="pdf")
    except Exception as exc:
        raise ExtractionFailed("CORRUPT_FILE", f"Cannot open PDF: {exc}") from exc

    with document:
        if document.needs_pass:
            raise ExtractionFailed("ENCRYPTED_PDF", "PDF is password-protected")
        if document.page_count == 0:
            raise ExtractionFailed("CORRUPT_FILE", "PDF has no pages")
        if document.page_count > limits.max_pages:
            raise ExtractionFailed(
                "TOO_MANY_PAGES", f"{document.page_count} pages exceeds {limits.max_pages}"
            )

        result = ExtractionResult(extractor=f"pymupdf {pymupdf.VersionBind}")
        for number in range(1, document.page_count + 1):
            page: pymupdf.Page = document[number - 1]
            result.pages.append(_extract_pdf_page(number, page, limits))
        return result


def _extract_pdf_page(number: int, page: pymupdf.Page, limits: ExtractionLimits) -> ExtractedPage:
    native = clean_text(page.get_text("text", sort=True))
    if len(native) >= limits.ocr_min_chars:
        return ExtractedPage(number, native, "TEXT")

    # Little or no text layer. If the page carries images it is probably scanned: OCR it.
    # Decided per page, so mixed documents (typed pages + scanned annexes) work.
    if page.get_images(full=False):
        ocr_text, confidence = ocr_image(_render(page, limits.ocr_dpi), limits.ocr_language)
        if len(ocr_text) > len(native):
            return ExtractedPage(number, ocr_text, "OCR", ocr_confidence=confidence)

    return ExtractedPage(number, native, "TEXT" if native else "EMPTY")


def _render(page: pymupdf.Page, dpi: int) -> Image.Image:
    width_in, height_in = page.rect.width / 72, page.rect.height / 72
    while dpi > 72 and (width_in * dpi) * (height_in * dpi) > MAX_RENDER_PIXELS:
        dpi -= 25
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


# --- DOCX --------------------------------------------------------------------------------------


def _check_zip_bomb(path: Path, limits: ExtractionLimits) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise ExtractionFailed("CORRUPT_FILE", "Not a valid DOCX archive") from exc
    if total > limits.max_decompressed_bytes:
        raise ExtractionFailed(
            "DECOMPRESSION_LIMIT",
            f"Archive expands to {total} bytes (limit {limits.max_decompressed_bytes})",
        )


def _table_text(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells = [clean_text(cell.text) for cell in row.cells]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_docx(path: Path, limits: ExtractionLimits) -> ExtractionResult:
    # Check sizes from the central directory *before* anything decompresses the archive.
    _check_zip_bomb(path, limits)
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractionFailed("CORRUPT_FILE", f"Cannot open DOCX: {exc}") from exc

    result = ExtractionResult(extractor=f"python-docx {docx.__version__}")
    section: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = clean_text("\n\n".join(buffer))
        if text:
            result.pages.append(ExtractedPage(len(result.pages) + 1, text, "TEXT", section=section))
        buffer.clear()

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = clean_text(block.text)
            style = (block.style.name if block.style is not None else "") or ""
            if style.startswith(("Heading", "Title")) and text:
                flush()  # a heading starts a new unit
                section = text[:500]
            if text:
                buffer.append(text)
        elif isinstance(block, Table):
            buffer.append(_table_text(block))
    flush()

    if len(result.pages) > limits.max_pages:
        raise ExtractionFailed("TOO_MANY_PAGES", f"{len(result.pages)} sections")
    if not result.pages:
        result.pages.append(ExtractedPage(1, "", "EMPTY"))
    return result


# --- TXT ---------------------------------------------------------------------------------------


def extract_text(path: Path) -> ExtractionResult:
    raw = path.read_bytes()  # bounded by the upload size limit
    # Strict UTF-8 first: by far the most common, and unambiguous when it decodes. Statistical
    # detection is the fallback, since single-byte code pages (cp1250 vs cp1252) are easy to
    # confuse on short texts.
    try:
        text, encoding = raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        best = from_path(path).best()
        if best is None:
            raise ExtractionFailed(
                "UNDECODABLE_TEXT", "Could not determine the text encoding"
            ) from None
        text, encoding = str(best), best.encoding
    text = clean_text(text)
    return ExtractionResult(
        pages=[ExtractedPage(1, text, "TEXT" if text else "EMPTY")],
        extractor=f"text ({encoding})",
    )


# --- Images ------------------------------------------------------------------------------------


def extract_image(path: Path, limits: ExtractionLimits) -> ExtractionResult:
    result = ExtractionResult(extractor="tesseract")
    try:
        with Image.open(path) as image:
            for number, frame in enumerate(ImageSequence.Iterator(image), start=1):  # TIFF pages
                if number > limits.max_pages:
                    raise ExtractionFailed("TOO_MANY_PAGES", f"More than {limits.max_pages}")
                text, confidence = ocr_image(frame.copy(), limits.ocr_language)
                result.pages.append(
                    ExtractedPage(
                        number, text, "OCR" if text else "EMPTY", ocr_confidence=confidence
                    )
                )
    except Image.DecompressionBombError as exc:
        raise ExtractionFailed("IMAGE_TOO_LARGE", str(exc)) from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ExtractionFailed("CORRUPT_FILE", f"Cannot open image: {exc}") from exc
    return result
