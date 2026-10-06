import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, Timestamps, UUIDPrimaryKey


class DocumentStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class Document(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("status IN ('QUEUED', 'PROCESSING', 'PROCESSED', 'FAILED')", name="status"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        # Serves the default list query: WHERE tenant_id = ? ORDER BY created_at DESC, id DESC
        Index("ix_documents_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
        # Duplicate detection is per tenant (cross-tenant dedup would leak file existence),
        # and ignores soft-deleted rows so a deleted file can be uploaded again.
        Index(
            "uq_documents_tenant_checksum_active",
            "tenant_id",
            "checksum",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str] = mapped_column(String(64))  # SHA-256 hex
    storage_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.QUEUED, server_default=DocumentStatus.QUEUED
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optimistic-locking counter: updates must name the version they read.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
