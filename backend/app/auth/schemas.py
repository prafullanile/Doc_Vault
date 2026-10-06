import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: str = Field(min_length=2, max_length=200)

    _email = field_validator("email")(_normalize_email)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    org_id: uuid.UUID | None = None  # defaults to the user's oldest membership

    _email = field_validator("email")(_normalize_email)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class SwitchOrgRequest(BaseModel):
    org_id: uuid.UUID


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int
    org_id: uuid.UUID
    role: str


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    role: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime


class MeResponse(BaseModel):
    user: UserOut
    active_org_id: uuid.UUID
    role: str
    memberships: list[MembershipOut]
