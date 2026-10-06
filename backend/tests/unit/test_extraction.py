import subprocess
import sys
from pathlib import Path

import pytest

from app.processing.extraction.extractors import DOCX_MIME, extract
from app.processing.extraction.ocr import tesseract_available
from app.processing.extraction.types import ExtractionFailed, ExtractionLimits
from app.processing.models import JobType
from app.processing.stages import PIPELINE, next_stage
from tests import factories

LIMITS = ExtractionLimits(
    max_pages=50,
    max_decompressed_bytes=10_000_000,
    ocr_language="eng",
    ocr_dpi=200,
    ocr_min_chars=25,
)
needs_tesseract = pytest.mark.skipif(not tesseract_available(), reason="Tesseract not installed")


def write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_pdf_text_layer_and_blank_pages(tmp_path):
    path = write(tmp_path, "a.pdf", factories.text_pdf([factories.SAMPLE_ENGLISH, "", "short"]))
    result = extract(path, "application/pdf", LIMITS)
    assert [(p.page_number, p.source) for p in result.pages] == [
        (1, "TEXT"),
        (2, "EMPTY"),
        (3, "TEXT"),  # short text, but no image on the page, so no OCR
    ]
    assert result.pages[2].text == "short"
    assert result.extractor.startswith("pymupdf")


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"%PDF-1.7\nnot a pdf", "CORRUPT_FILE"),
        (factories.encrypted_pdf(), "ENCRYPTED_PDF"),
    ],
)
def test_pdf_permanent_failures(tmp_path, content, code):
    with pytest.raises(ExtractionFailed) as err:
        extract(write(tmp_path, "x.pdf", content), "application/pdf", LIMITS)
    assert err.value.code == code


def test_pdf_page_limit(tmp_path):
    limits = ExtractionLimits(**{**LIMITS.__dict__, "max_pages": 2})
    path = write(tmp_path, "big.pdf", factories.text_pdf(["x"] * 3))
    with pytest.raises(ExtractionFailed) as err:
        extract(path, "application/pdf", limits)
    assert err.value.code == "TOO_MANY_PAGES"


def test_docx_sections_and_tables(tmp_path):
    content = factories.docx_file(
        [("Scope", "Covers all services."), ("Liability", "Capped at one million.")],
        table=[["Clause", "Limit"], ["4.2", "1M"]],
    )
    result = extract(write(tmp_path, "c.docx", content), DOCX_MIME, LIMITS)
    assert [(p.page_number, p.section) for p in result.pages] == [(1, "Scope"), (2, "Liability")]
    assert result.pages[1].text.endswith("Clause | Limit\n4.2 | 1M")


def test_docx_zip_bomb_is_rejected_before_decompression(tmp_path):
    limits = ExtractionLimits(**{**LIMITS.__dict__, "max_decompressed_bytes": 100_000})
    path = write(tmp_path, "bomb.docx", factories.zip_bomb_docx(1_000_000))
    with pytest.raises(ExtractionFailed) as err:
        extract(path, DOCX_MIME, limits)
    assert err.value.code == "DECOMPRESSION_LIMIT"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Grüße aus München — naïve café".encode(), "Grüße aus München — naïve café"),
        ("﻿With a BOM".encode(), "With a BOM"),
        (b"Nul\x00bytes\x00removed", "Nulbytesremoved"),
    ],
)
def test_text_decoding(tmp_path, raw, expected):
    result = extract(write(tmp_path, "t.txt", raw), "text/plain", LIMITS)
    assert result.pages[0].text == expected


def test_non_utf8_text_falls_back_to_detection(tmp_path):
    raw = ("Le café est très bon et la façade est belle. " * 30).encode("cp1252")
    result = extract(write(tmp_path, "t.txt", raw), "text/plain", LIMITS)
    assert "caf" in result.pages[0].text
    assert "utf-8" not in result.extractor


def test_unsupported_type(tmp_path):
    with pytest.raises(ExtractionFailed) as err:
        extract(write(tmp_path, "x.bin", b"x"), "application/zip", LIMITS)
    assert err.value.code == "UNSUPPORTED_FILE_TYPE"


@needs_tesseract
def test_ocr_on_scanned_pdf_and_image(tmp_path):
    scanned = extract(
        write(tmp_path, "s.pdf", factories.scanned_pdf("Invoice total\n4200 dollars")),
        "application/pdf",
        LIMITS,
    )
    assert scanned.pages[0].source == "OCR"
    assert "Invoice" in scanned.pages[0].text
    image = extract(write(tmp_path, "i.png", factories.png("Hello OCR world")), "image/png", LIMITS)
    assert "OCR" in image.pages[0].text


@pytest.mark.skipif(tesseract_available(), reason="Only meaningful without Tesseract")
def test_scanned_pdf_without_tesseract_fails_clearly(tmp_path):
    with pytest.raises(ExtractionFailed) as err:
        extract(write(tmp_path, "s.pdf", factories.scanned_pdf("Hello")), "application/pdf", LIMITS)
    assert err.value.code == "OCR_UNAVAILABLE"


def test_pipeline_order():
    assert PIPELINE == (JobType.EXTRACT_TEXT, JobType.DETECT_LANGUAGE)
    assert next_stage(JobType.EXTRACT_TEXT) == JobType.DETECT_LANGUAGE
    assert next_stage(JobType.DETECT_LANGUAGE) is None


@pytest.mark.parametrize("entry_module", ["app.worker.__main__", "app.main"])
def test_entry_points_register_every_model(entry_module):
    """Each process must import all models, or SQLAlchemy can't resolve cross-table foreign
    keys at flush time. Checked in a fresh interpreter, since tests import everything."""
    code = f"import {entry_module}; from app.database.base import Base; Base.metadata.sorted_tables"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
