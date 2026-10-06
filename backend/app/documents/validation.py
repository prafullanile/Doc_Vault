"""Upload validation. The file's bytes decide its type — never the client's extension or
Content-Type header, which are trivially spoofed (§40: uploaded files must not be trusted)."""

import codecs
import hashlib
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

from app.common.errors import PayloadTooLarge, UnprocessableEntity, UnsupportedMediaType

HEAD_BYTES = 8192
CHUNK_BYTES = 1024 * 1024

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class FileType:
    mime_type: str
    extension: str


PDF = FileType("application/pdf", "pdf")
DOCX = FileType(DOCX_MIME, "docx")
TEXT = FileType("text/plain", "txt")
PNG = FileType("image/png", "png")
JPEG = FileType("image/jpeg", "jpg")
TIFF = FileType("image/tiff", "tiff")


@dataclass(frozen=True)
class FileDigest:
    sha256: str
    size_bytes: int
    head: bytes


def digest_file(file: BinaryIO, max_bytes: int) -> FileDigest:
    """Streams the file once: SHA-256, size limit and the first bytes for type sniffing."""
    file.seek(0)
    sha = hashlib.sha256()
    size = 0
    head = b""
    while chunk := file.read(CHUNK_BYTES):
        if not head:
            head = chunk[:HEAD_BYTES]
        size += len(chunk)
        if size > max_bytes:
            raise PayloadTooLarge(details={"max_bytes": max_bytes})
        sha.update(chunk)
    if size == 0:
        raise UnprocessableEntity("File is empty", code="EMPTY_FILE")
    return FileDigest(sha256=sha.hexdigest(), size_bytes=size, head=head)


def _is_docx(file: BinaryIO) -> bool:
    try:
        file.seek(0)
        with zipfile.ZipFile(file) as archive:  # reads only the central directory
            return "word/document.xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        # Incremental decode tolerates a multi-byte character cut off at the end of `head`.
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def detect_file_type(head: bytes, file: BinaryIO) -> FileType:
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return TIFF
    if head.startswith(b"PK\x03\x04"):
        if _is_docx(file):
            return DOCX
        raise UnsupportedMediaType("ZIP archives other than DOCX are not supported")
    if _looks_like_text(head):
        return TEXT
    raise UnsupportedMediaType(
        details={"supported": [t.mime_type for t in (PDF, DOCX, TEXT, PNG, JPEG, TIFF)]}
    )


_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f<>:\"|?*]")


def sanitize_filename(raw: str | None) -> str:
    """Display name only (never used as a path): drop directories and control characters."""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_FILENAME_CHARS.sub("", name).strip(" .")
    if not name:
        return "document"
    if len(name) > 255:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 254 - len(ext)] + dot + ext) if dot and len(ext) <= 16 else name[:255]
    return name
