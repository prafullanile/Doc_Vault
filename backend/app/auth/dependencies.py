import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import jwt
import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import decode_access_token
from app.common.errors import Forbidden, Unauthorized
from app.core.dependencies import AppSettings, DbSession
from app.database.session import bind_tenant
from app.organizations.models import Membership, Role

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: Role


async def get_principal(
    settings: AppSettings,
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise Unauthorized()
    try:
        claims = decode_access_token(settings, credentials.credentials)
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("Invalid or expired access token", code="INVALID_TOKEN") from exc

    # The token proves identity; the role always comes from the database, so a removed or
    # demoted member loses access immediately rather than when the token expires.
    role = await session.scalar(
        select(Membership.role)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.user_id == claims.user_id,
            Membership.org_id == claims.org_id,
            User.is_active.is_(True),
        )
    )
    if role is None:
        raise Unauthorized("Membership is no longer valid", code="INVALID_TOKEN")

    structlog.contextvars.bind_contextvars(
        user_id=str(claims.user_id), tenant_id=str(claims.org_id)
    )
    return Principal(user_id=claims.user_id, org_id=claims.org_id, role=Role(role))


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def get_tenant_session(principal: CurrentPrincipal, session: DbSession) -> AsyncSession:
    """The request's session, bound to the caller's tenant so RLS applies to every query."""
    await bind_tenant(session, principal.org_id)
    return session


TenantSession = Annotated[AsyncSession, Depends(get_tenant_session)]


def require_role(*allowed: Role) -> Callable[[Principal], Awaitable[Principal]]:
    async def _check(principal: CurrentPrincipal) -> Principal:
        if principal.role not in allowed:
            raise Forbidden()
        return principal

    return _check


# Role sets used across routers.
ANY_MEMBER = (Role.ADMIN, Role.ANALYST, Role.USER, Role.VIEWER)
WRITERS = (Role.ADMIN, Role.ANALYST, Role.USER)
ADMINS = (Role.ADMIN,)

Reader = Annotated[Principal, Depends(require_role(*ANY_MEMBER))]
Writer = Annotated[Principal, Depends(require_role(*WRITERS))]
Admin = Annotated[Principal, Depends(require_role(*ADMINS))]
