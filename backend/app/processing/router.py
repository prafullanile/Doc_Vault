import uuid

from fastapi import APIRouter

from app.auth.dependencies import Reader, TenantSession, Writer
from app.processing import service
from app.processing.schemas import JobDetailOut

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: uuid.UUID, principal: Reader, session: TenantSession) -> JobDetailOut:
    return await service.get_job(session, principal, job_id)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: uuid.UUID, principal: Writer, session: TenantSession) -> JobDetailOut:
    return await service.cancel_job(session, principal, job_id)
