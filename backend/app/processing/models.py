"""The PostgreSQL job queue. PostgreSQL is the source of truth for processing state
(ADR-0004): workers claim rows with FOR UPDATE SKIP LOCKED and hold them under a lease."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, Timestamps, UUIDPrimaryKey


class JobType(StrEnum):
    EXTRACT_TEXT = "EXTRACT_TEXT"
    DETECT_LANGUAGE = "DETECT_LANGUAGE"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"  # failed, waiting for next_attempt_at
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"  # permanent error: retrying cannot help
    CANCELLED = "CANCELLED"
    DEAD_LETTER = "DEAD_LETTER"  # ran out of attempts; needs a human


ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRYING)
CLAIMABLE_STATUSES = (JobStatus.QUEUED, JobStatus.RETRYING)


class AttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    PERMANENT_ERROR = "PERMANENT_ERROR"
    LEASE_EXPIRED = "LEASE_EXPIRED"  # the worker died or stalled; the reaper took it back
    CANCELLED = "CANCELLED"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


_ACTIVE_SQL = f"status IN ({_in(ACTIVE_STATUSES)})"


class ProcessingJob(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(tuple(JobStatus))})", name="status"),
        CheckConstraint(f"job_type IN ({_in(tuple(JobType))})", name="job_type"),
        # The claim query: claimable jobs, lowest priority number first, then oldest due.
        Index(
            "ix_processing_jobs_claimable",
            "priority",
            "next_attempt_at",
            postgresql_where=f"status IN ({_in(CLAIMABLE_STATUSES)})",
        ),
        # The reaper: running jobs whose lease ran out.
        Index(
            "ix_processing_jobs_running_lease",
            "locked_until",
            postgresql_where="status = 'RUNNING'",
        ),
        # At most one live job per stage per version, so duplicate events or double clicks on
        # "reprocess" can never run the same stage twice concurrently.
        Index(
            "uq_processing_jobs_active_stage",
            "document_version_id",
            "job_type",
            unique=True,
            postgresql_where=_ACTIVE_SQL,
        ),
        Index("ix_processing_jobs_document", "tenant_id", "document_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    document_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document_versions.id"))
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(16), default=JobStatus.QUEUED, server_default=JobStatus.QUEUED
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttempt(Base):
    """One row per execution attempt — the job's history, for debugging and metrics."""

    __tablename__ = "job_attempts"
    __table_args__ = (
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_in(tuple(AttemptOutcome))})", name="outcome"
        ),
        Index("ix_job_attempts_job", "job_id", "attempt"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("processing_jobs.id", ondelete="CASCADE"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    attempt: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))
