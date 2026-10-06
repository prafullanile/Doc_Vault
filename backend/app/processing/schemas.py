import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.processing.models import JobStatus, JobType


class JobAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt: int
    worker_id: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    error_code: str | None
    error_message: str | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    job_type: JobType
    status: JobStatus
    attempt: int
    max_attempts: int
    next_attempt_at: datetime
    cancel_requested: bool
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class JobDetailOut(JobOut):
    attempts: list[JobAttemptOut]
