import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.organizations.models import Role


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    role: Role  # the caller's role in this organization


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: Role
    joined_at: datetime


class AddMemberRequest(BaseModel):
    email: EmailStr
    role: Role = Role.USER

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return value.strip().lower()


class UpdateMemberRequest(BaseModel):
    role: Role


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    details: dict[str, Any]
    request_id: str | None
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    next_before_id: int | None = Field(description="Pass as before_id to fetch the next page")
