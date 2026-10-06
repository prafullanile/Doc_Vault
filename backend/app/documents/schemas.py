import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.documents.models import Document, DocumentStatus, DocumentVersion, PageSource
from app.processing.schemas import JobOut


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    status: DocumentStatus
    # From the current version:
    current_version_no: int
    mime_type: str
    size_bytes: int
    checksum: str
    page_count: int | None
    language: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    version: int = Field(description="Optimistic-locking counter; send it back on PATCH")

    @classmethod
    def build(cls, document: Document, current: DocumentVersion) -> "DocumentOut":
        return cls(
            id=document.id,
            filename=document.filename,
            status=DocumentStatus(document.status),
            current_version_no=current.version_no,
            mime_type=current.mime_type,
            size_bytes=current.size_bytes,
            checksum=current.checksum,
            page_count=current.page_count,
            language=current.language,
            created_by=document.created_by,
            created_at=document.created_at,
            updated_at=document.updated_at,
            version=document.version,
        )


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_no: int
    filename: str
    mime_type: str
    size_bytes: int
    checksum: str
    status: DocumentStatus
    page_count: int | None
    ocr_page_count: int | None
    language: str | None
    error_code: str | None
    pipeline_version: str | None
    created_at: datetime
    processed_at: datetime | None


class DocumentUploadResponse(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    job_id: uuid.UUID
    status: DocumentStatus


class DocumentStatusOut(BaseModel):
    document_id: uuid.UUID
    status: DocumentStatus
    current_version: VersionOut
    jobs: list[JobOut] = Field(description="Jobs for the current version, oldest first")


class PageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    page_number: int
    section: str | None
    source: PageSource
    ocr_confidence: float | None
    char_count: int
    text: str


class PagesOut(BaseModel):
    version_no: int
    items: list[PageOut]
    next_cursor: int | None = Field(description="Pass as `after` to fetch the next pages")


class DocumentUpdate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1, description="The version you read; a mismatch returns 409")


class SortOrder(StrEnum):
    NEWEST = "-created_at"
    OLDEST = "created_at"
