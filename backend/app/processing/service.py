import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.common.errors import Conflict, NotFound
from app.documents.models import DocumentStatus
from app.processing import queue
from app.processing.models import JobAttempt, JobStatus, ProcessingJob
from app.processing.schemas import JobAttemptOut, JobDetailOut, JobOut


async def _get_job(session: AsyncSession, principal: Principal, job_id: uuid.UUID) -> ProcessingJob:
    job = await session.scalar(
        select(ProcessingJob).where(
            ProcessingJob.id == job_id, ProcessingJob.tenant_id == principal.org_id
        )
    )
    if job is None:
        raise NotFound("Job does not exist", code="JOB_NOT_FOUND")
    return job


async def get_job(session: AsyncSession, principal: Principal, job_id: uuid.UUID) -> JobDetailOut:
    job = await _get_job(session, principal, job_id)
    attempts = await session.scalars(
        select(JobAttempt)
        .where(JobAttempt.job_id == job.id, JobAttempt.tenant_id == principal.org_id)
        .order_by(JobAttempt.attempt)
    )
    return JobDetailOut(
        **JobOut.model_validate(job).model_dump(),
        attempts=[JobAttemptOut.model_validate(a) for a in attempts],
    )


async def cancel_job(
    session: AsyncSession, principal: Principal, job_id: uuid.UUID
) -> JobDetailOut:
    job = await _get_job(session, principal, job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.RETRYING):
        # Guarded on status so a worker claiming it at this instant wins cleanly.
        cancelled = await session.scalar(
            update(ProcessingJob)
            .where(ProcessingJob.id == job.id, ProcessingJob.status == job.status)
            .values(status=JobStatus.CANCELLED, completed_at=func.now(), updated_at=func.now())
            .returning(ProcessingJob.id)
        )
        if cancelled is None:
            raise Conflict("Job state changed; retry the request", code="JOB_STATE_CHANGED")
        await queue.update_version_status(
            session,
            tenant_id=job.tenant_id,
            document_id=job.document_id,
            version_id=job.document_version_id,
            status=DocumentStatus.FAILED,
            error_code="CANCELLED",
        )
    elif job.status == JobStatus.RUNNING:
        # The worker sees the flag at its next heartbeat and stops the stage.
        job.cancel_requested = True
    else:
        raise Conflict(f"Job is already {job.status}", code="JOB_NOT_ACTIVE")

    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="job.cancel_requested",
        target_type="job",
        target_id=job.id,
    )
    await session.commit()
    return await get_job(session, principal, job_id)
