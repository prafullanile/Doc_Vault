import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.audit import service as audit
from app.auth.dependencies import Admin, Reader, TenantSession
from app.organizations import service
from app.organizations.schemas import (
    AddMemberRequest,
    AuditLogOut,
    AuditLogPage,
    MemberOut,
    OrganizationOut,
    UpdateMemberRequest,
)

# "current" is the organization selected by the access token. Operating only on it means a
# client can never address another tenant's organization by ID.
router = APIRouter(prefix="/organizations/current", tags=["organizations"])


@router.get("")
async def get_current(principal: Reader, session: TenantSession) -> OrganizationOut:
    return await service.get_current(session, principal)


@router.get("/members")
async def list_members(principal: Reader, session: TenantSession) -> list[MemberOut]:
    return await service.list_members(session, principal)


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def add_member(body: AddMemberRequest, principal: Admin, session: TenantSession) -> MemberOut:
    return await service.add_member(session, principal, body.email, body.role)


@router.patch("/members/{user_id}")
async def update_member(
    user_id: uuid.UUID, body: UpdateMemberRequest, principal: Admin, session: TenantSession
) -> MemberOut:
    return await service.update_member_role(session, principal, user_id, body.role)


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: uuid.UUID, principal: Admin, session: TenantSession) -> Response:
    await service.remove_member(session, principal, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/audit-logs")
async def list_audit_logs(
    principal: Admin,
    session: TenantSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> AuditLogPage:
    rows = await audit.list_logs(
        session, tenant_id=principal.org_id, limit=limit + 1, before_id=before_id
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return AuditLogPage(
        items=[AuditLogOut.model_validate(r) for r in rows],
        next_before_id=rows[-1].id if has_more else None,
    )
