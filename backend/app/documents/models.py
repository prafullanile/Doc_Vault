import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, Timestamps, UUIDPrimaryKey


class DocumentStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class PageSource(StrEnum):
    TEXT = "TEXT"  # extracted from the file's own text layer
    OCR = "OCR"  # recognised from a rendered image
    EMPTY = "EMPTY"  # no text found either way (blank page, picture without words)


_STATUS_CHECK = "status IN ('QUEUED', 'PROCESSING', 'PROCESSED', 'FAILED')"


class Document(UUIDPrimaryKey, Timestamps, Base):
    """The logical document. Its content lives in versions; `current_version_id` is the newest."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="status"),
        # Serves the default list query: WHERE tenant_id = ? ORDER BY created_at DESC, id DESC
        Index("ix_documents_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    filename: Mapped[str] = mapped_column(String(255))
    # Mirrors the current version's status, so lists can filter without a join.
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.QUEUED, server_default=DocumentStatus.QUEUED
    )
    # use_alter: documents ↔ document_versions reference each other.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", use_alter=True)
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optimistic-locking counter: updates must name the version they read.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class DocumentVersion(UUIDPrimaryKey, Base):
    """One uploaded file. A new upload of an existing document adds a version; nothing is
    overwritten, which is what document comparison (§28) will need."""

    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="status"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_document_no"),
        # Duplicate detection is per tenant (cross-tenant dedup would leak file existence),
        # and ignores deleted documents so a deleted file can be uploaded again.
        Index(
            "uq_document_versions_tenant_checksum_active",
            "tenant_id",
            "checksum",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(64))  # SHA-256 hex
    storage_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.QUEUED, server_default=DocumentStatus.QUEUED
    )
    # Filled in by the processing pipeline.
    pipeline_version: Mapped[str | None] = mapped_column(String(32))
    page_count: Mapped[int | None] = mapped_column(Integer)
    ocr_page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(16))
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentPage(Base):
    """Extracted text, one row per page — page numbers are what citations will point at.

    Formats without real pages (DOCX, TXT) are split into sequential units: DOCX by heading,
    with the heading stored in `section`; TXT as a single unit.
    """

    __tablename__ = "document_pages"
    __table_args__ = (CheckConstraint("source IN ('TEXT', 'OCR', 'EMPTY')", name="source"),)

    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), primary_key=True
    )
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    section: Mapped[str | None] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8))
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    char_count: Mapped[int] = mapped_column(Integer)
