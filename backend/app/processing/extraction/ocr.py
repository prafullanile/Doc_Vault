from functools import lru_cache

import pytesseract
from PIL import Image

from app.processing.extraction.types import ExtractionFailed, clean_text

# Refuse absurd images before decoding them (decompression bombs).
Image.MAX_IMAGE_PIXELS = 120_000_000


@lru_cache
def tesseract_available() -> bool:
    try:
        pytesseract.get_tesseract_version()
    except (pytesseract.TesseractNotFoundError, OSError):
        return False
    return True


def ocr_image(image: Image.Image, language: str) -> tuple[str, float | None]:
    """Returns (text, mean word confidence in 0..1). One Tesseract pass: text is rebuilt from
    the word boxes, keeping line and paragraph breaks."""
    if not tesseract_available():
        raise ExtractionFailed(
            "OCR_UNAVAILABLE", "This page needs OCR but Tesseract is not installed"
        )
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    data = pytesseract.image_to_data(image, lang=language, output_type=pytesseract.Output.DICT)

    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for i, word in enumerate(data["text"]):
        if not word or not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        conf = float(data["conf"][i])
        if conf >= 0:
            confidences.append(conf)

    paragraphs: dict[tuple[int, int], list[str]] = {}
    for (block, par, _), words in lines.items():
        paragraphs.setdefault((block, par), []).append(" ".join(words))
    text = "\n\n".join("\n".join(par_lines) for par_lines in paragraphs.values())
    confidence = round(sum(confidences) / len(confidences) / 100, 4) if confidences else None
    return clean_text(text), confidence
