import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditLog
from app.common.request_context import get_request_id


def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Stage an audit row in the caller's transaction, so it commits (or not) with the change."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
            request_id=get_request_id(),
        )
    )


async def list_logs(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int, before_id: int | None
) -> list[AuditLog]:
    # The explicit filter is the first line of defence; RLS on the tenant-bound session is the
    # backstop if a query ever forgets it.
    query = (
        select(AuditLog)
        .where(AuditLog.tenant_id == tenant_id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    return list((await session.scalars(query)).all())
