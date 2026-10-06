"""Job queue operations on PostgreSQL (ADR-0004).

Claiming uses ``FOR UPDATE SKIP LOCKED``: concurrent workers never block on, or double-claim,
the same row. A claimed job is held under a *lease* (``locked_until``) that the worker
extends by heartbeating. If the worker dies, the lease runs out and the reaper requeues
the job.

Every state change made by a worker is *fenced*: it only applies if the row still says
``RUNNING``, locked by this worker, at this attempt number. A worker that stalled past its
lease (and whose job was handed to someone else) can therefore never overwrite the newer
attempt's outcome.
"""

import random
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import ColumnElement, and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import bind_tenant
from app.documents.models import Document, DocumentStatus, DocumentVersion
from app.processing.models import (
    CLAIMABLE_STATUSES,
    AttemptOutcome,
    JobAttempt,
    JobStatus,
    JobType,
    ProcessingJob,
)


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    job_type: JobType
    attempt: int
    max_attempts: int
    worker_id: str


def backoff_seconds(attempt: int, base: float, cap: float, rand: float | None = None) -> float:
    """Capped exponential backoff with "equal jitter": half fixed, half random. The jitter
    spreads out retries so a recovering dependency isn't hit by every job at once."""
    ceiling = min(cap, base * 2 ** max(attempt - 1, 0))
    r = random.random() if rand is None else rand  # noqa: S311 - not cryptographic
    return float(ceiling / 2 + r * ceiling / 2)


def _truncate(message: str | None, limit: int = 1000) -> str | None:
    return None if message is None else message[:limit]


# --- enqueue -----------------------------------------------------------------------------------


def enqueue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    job_type: JobType,
    max_attempts: int,
    priority: int = 100,
) -> ProcessingJob:
    """Stage a job in the caller's transaction, so it commits atomically with whatever made it
    necessary (an upload, the previous stage's results). The partial unique index rejects a
    second live job for the same stage of the same version."""
    job = ProcessingJob(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=version_id,
        job_type=job_type,
        status=JobStatus.QUEUED,
        priority=priority,
        attempt=0,
        max_attempts=max_attempts,
    )
    session.add(job)
    return job


# --- worker side -------------------------------------------------------------------------------


async def claim(session: AsyncSession, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    candidate = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.status.in_(CLAIMABLE_STATUSES),
            ProcessingJob.next_attempt_at <= func.now(),
            ProcessingJob.cancel_requested.is_(False),
        )
        .order_by(ProcessingJob.priority, ProcessingJob.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    row = await session.scalar(
        update(ProcessingJob)
        .where(ProcessingJob.id == candidate)
        .values(
            status=JobStatus.RUNNING,
            locked_by=worker_id,
            locked_until=func.now() + timedelta(seconds=lease_seconds),
            attempt=ProcessingJob.attempt + 1,
            started_at=func.coalesce(ProcessingJob.started_at, func.now()),
            error_code=None,
            error_message=None,
            updated_at=func.now(),
        )
        .returning(ProcessingJob)
    )
    if row is None:
        await session.rollback()
        return None

    job = ClaimedJob(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        version_id=row.document_version_id,
        job_type=JobType(row.job_type),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        worker_id=worker_id,
    )
    session.add(
        JobAttempt(job_id=job.id, tenant_id=job.tenant_id, attempt=job.attempt, worker_id=worker_id)
    )
    await set_version_status(session, job, DocumentStatus.PROCESSING)
    await session.commit()
    return job


def _owned_by(job: ClaimedJob) -> ColumnElement[bool]:
    return and_(
        ProcessingJob.id == job.id,
        ProcessingJob.status == JobStatus.RUNNING,
        ProcessingJob.locked_by == job.worker_id,
        ProcessingJob.attempt == job.attempt,
    )


async def heartbeat(
    session: AsyncSession, job: ClaimedJob, lease_seconds: int
) -> tuple[bool, bool]:
    """Extends the lease. Returns (still_owned, cancel_requested)."""
    cancel_requested = await session.scalar(
        update(ProcessingJob)
        .where(_owned_by(job))
        .values(locked_until=func.now() + timedelta(seconds=lease_seconds), updated_at=func.now())
        .returning(ProcessingJob.cancel_requested)
    )
    await session.commit()
    if cancel_requested is None:
        return False, False
    return True, bool(cancel_requested)


async def _finish_attempt(
    session: AsyncSession,
    job: ClaimedJob,
    outcome: AttemptOutcome,
    code: str | None = None,
    message: str | None = None,
) -> None:
    await session.execute(
        update(JobAttempt)
        .where(
            JobAttempt.job_id == job.id,
            JobAttempt.attempt == job.attempt,
            JobAttempt.finished_at.is_(None),
        )
        .values(
            finished_at=func.now(),
            outcome=outcome,
            error_code=code,
            error_message=_truncate(message),
        )
    )


async def mark_completed(session: AsyncSession, job: ClaimedJob) -> bool:
    """First step of the completion transaction. False means the lease was lost: the caller
    must roll back and discard its results."""
    updated = await session.scalar(
        update(ProcessingJob)
        .where(_owned_by(job))
        .values(
            status=JobStatus.COMPLETED,
            completed_at=func.now(),
            locked_by=None,
            locked_until=None,
            updated_at=func.now(),
        )
        .returning(ProcessingJob.id)
    )
    if updated is None:
        return False
    await _finish_attempt(session, job, AttemptOutcome.SUCCEEDED)
    return True


async def mark_failed(
    session: AsyncSession,
    job: ClaimedJob,
    *,
    code: str,
    message: str,
    permanent: bool,
    retry_in_seconds: float,
) -> JobStatus | None:
    """Records a failed attempt. Returns the new status, or None if the lease was lost."""
    if permanent:
        status = JobStatus.FAILED
    elif job.attempt >= job.max_attempts:
        status = JobStatus.DEAD_LETTER
    else:
        status = JobStatus.RETRYING

    updated = await session.scalar(
        update(ProcessingJob)
        .where(_owned_by(job))
        .values(
            status=status,
            locked_by=None,
            locked_until=None,
            error_code=code,
            error_message=_truncate(message),
            next_attempt_at=func.now() + timedelta(seconds=retry_in_seconds),
            completed_at=func.now() if status != JobStatus.RETRYING else None,
            updated_at=func.now(),
        )
        .returning(ProcessingJob.id)
    )
    if updated is None:
        await session.rollback()
        return None
    outcome = AttemptOutcome.PERMANENT_ERROR if permanent else AttemptOutcome.RETRYABLE_ERROR
    await _finish_attempt(session, job, outcome, code, message)
    if status != JobStatus.RETRYING:
        await set_version_status(session, job, DocumentStatus.FAILED, error_code=code)
    await session.commit()
    return status


async def mark_cancelled(session: AsyncSession, job: ClaimedJob) -> bool:
    updated = await session.scalar(
        update(ProcessingJob)
        .where(_owned_by(job))
        .values(
            status=JobStatus.CANCELLED,
            locked_by=None,
            locked_until=None,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .returning(ProcessingJob.id)
    )
    if updated is None:
        await session.rollback()
        return False
    await _finish_attempt(session, job, AttemptOutcome.CANCELLED)
    await set_version_status(session, job, DocumentStatus.FAILED, error_code="CANCELLED")
    await session.commit()
    return True


async def reap_expired(session: AsyncSession, limit: int = 100) -> int:
    """Takes back jobs whose lease ran out (worker crashed, hung or lost its connection)."""
    expired = (
        select(ProcessingJob.id)
        .where(ProcessingJob.status == JobStatus.RUNNING, ProcessingJob.locked_until < func.now())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    new_status = case(
        (ProcessingJob.cancel_requested, JobStatus.CANCELLED),
        (ProcessingJob.attempt >= ProcessingJob.max_attempts, JobStatus.DEAD_LETTER),
        else_=JobStatus.RETRYING,
    )
    rows = (
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id.in_(expired))
            .values(
                status=new_status,
                locked_by=None,
                locked_until=None,
                next_attempt_at=func.now(),
                error_code="LEASE_EXPIRED",
                error_message="Worker stopped heartbeating before finishing",
                updated_at=func.now(),
            )
            .returning(
                ProcessingJob.id,
                ProcessingJob.tenant_id,
                ProcessingJob.document_id,
                ProcessingJob.document_version_id,
                ProcessingJob.job_type,
                ProcessingJob.attempt,
                ProcessingJob.max_attempts,
                ProcessingJob.status,
            )
        )
    ).all()

    for job_id, tenant_id, document_id, version_id, job_type, attempt, max_attempts, status in rows:
        job = ClaimedJob(
            job_id, tenant_id, document_id, version_id, JobType(job_type), attempt, max_attempts, ""
        )
        await _finish_attempt(
            session, job, AttemptOutcome.LEASE_EXPIRED, "LEASE_EXPIRED", "Lease expired"
        )
        if status in (JobStatus.DEAD_LETTER, JobStatus.CANCELLED):
            code = "LEASE_EXPIRED" if status == JobStatus.DEAD_LETTER else "CANCELLED"
            await set_version_status(session, job, DocumentStatus.FAILED, error_code=code)
    await session.commit()
    return len(rows)


# --- document state ----------------------------------------------------------------------------


async def set_version_status(
    session: AsyncSession,
    job: ClaimedJob,
    status: DocumentStatus,
    *,
    error_code: str | None = None,
    pipeline_version: str | None = None,
) -> None:
    await update_version_status(
        session,
        tenant_id=job.tenant_id,
        document_id=job.document_id,
        version_id=job.version_id,
        status=status,
        error_code=error_code,
        pipeline_version=pipeline_version,
    )


async def update_version_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    status: DocumentStatus,
    error_code: str | None = None,
    pipeline_version: str | None = None,
) -> None:
    """Updates the version and, if it is the document's current version, the document.
    Binds the tenant first, so RLS applies to the worker exactly as it does to the API."""
    await bind_tenant(session, tenant_id)
    values: dict[str, object] = {"status": status, "error_code": error_code}
    if status == DocumentStatus.PROCESSED:
        values["processed_at"] = func.now()
        values["pipeline_version"] = pipeline_version
    await session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version_id, DocumentVersion.tenant_id == tenant_id)
        .values(**values)
    )
    await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
            Document.current_version_id == version_id,
        )
        .values(status=status, updated_at=func.now())
    )
