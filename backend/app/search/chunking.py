"""Splits extracted pages into overlapping chunks for embedding and keyword search.

- Chunks never cross a page boundary, so every chunk cites exactly one page (§16, §27).
- Breaks fall on sentence boundaries where possible. Text without punctuation (tables, OCR
  output) falls back to splitting on words.
- Consecutive chunks of a page overlap by ~``overlap_words``, so a fact straddling a
  boundary is still found whole in at least one chunk.
"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

CHUNKER_VERSION = "sentences-v1"
TOKENS_PER_WORD = 1.3  # rough English average; used for an estimate, not a hard limit

_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|\n+")


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str
    section: str | None = None


@dataclass(frozen=True)
class Chunk:
    position: int  # order within the document version
    page_number: int
    section: str | None
    text: str
    token_count: int  # estimated


def estimate_tokens(words: int) -> int:
    return math.ceil(words * TOKENS_PER_WORD)


def _units(text: str, max_words: int) -> list[list[str]]:
    """Sentences as word lists; oversized sentences are cut into max_words pieces."""
    units: list[list[str]] = []
    for sentence in _SENTENCE_END.split(text):
        words = sentence.split()
        for start in range(0, len(words), max_words):
            piece = words[start : start + max_words]
            if piece:
                units.append(piece)
    return units


def _chunk_page(page: PageText, target_words: int, overlap_words: int) -> list[list[str]]:
    units = _units(page.text, max_words=target_words)
    chunks: list[list[str]] = []
    current: list[list[str]] = []
    size = 0
    for unit in units:
        if current and size + len(unit) > target_words:
            chunks.append([w for u in current for w in u])
            # Start the next chunk with the trailing sentences that fit in the overlap.
            carried: list[list[str]] = []
            carried_size = 0
            for previous in reversed(current):
                if carried_size + len(previous) > overlap_words:
                    break
                carried.insert(0, previous)
                carried_size += len(previous)
            current, size = carried, carried_size
        current.append(unit)
        size += len(unit)
    if current:  # always holds at least one new sentence beyond the carried overlap
        chunks.append([w for u in current for w in u])
    return chunks


def chunk_pages(pages: Iterable[PageText], target_words: int, overlap_words: int) -> list[Chunk]:
    if overlap_words >= target_words:
        raise ValueError("overlap_words must be smaller than target_words")
    chunks: list[Chunk] = []
    for page in pages:
        for words in _chunk_page(page, target_words, overlap_words):
            chunks.append(
                Chunk(
                    position=len(chunks),
                    page_number=page.page_number,
                    section=page.section,
                    text=" ".join(words),
                    token_count=estimate_tokens(len(words)),
                )
            )
    return chunks
