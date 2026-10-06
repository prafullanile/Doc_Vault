import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.documents.models import DocumentStatus


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    checksum: str
    status: DocumentStatus
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    version: int


class DocumentUploadResponse(BaseModel):
    document_id: uuid.UUID
    status: DocumentStatus


class DocumentUpdate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1, description="The version you read; a mismatch returns 409")


class SortOrder(StrEnum):
    NEWEST = "-created_at"
    OLDEST = "created_at"
