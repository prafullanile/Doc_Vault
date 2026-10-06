from dataclasses import asdict, dataclass, field
from typing import Any


class ExtractionFailed(Exception):
    """A permanent, file-specific failure. Crosses the subprocess boundary as exit code 3."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ExtractionLimits:
    max_pages: int
    max_decompressed_bytes: int
    ocr_language: str
    ocr_dpi: int
    ocr_min_chars: int


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    source: str  # PageSource value
    section: str | None = None
    ocr_confidence: float | None = None


@dataclass
class ExtractionResult:
    pages: list[ExtractedPage] = field(default_factory=list)
    extractor: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for p in self.pages if p.source == "OCR")

    def to_json(self) -> dict[str, Any]:
        return {"extractor": self.extractor, "pages": [asdict(p) for p in self.pages]}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ExtractionResult":
        return cls(
            pages=[ExtractedPage(**p) for p in data["pages"]], extractor=str(data["extractor"])
        )


def clean_text(text: str) -> str:
    # PostgreSQL text cannot hold NUL bytes; PDFs occasionally contain them.
    return text.replace("\x00", "").strip()
