import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.models import RefreshToken, User
from app.auth.schemas import (
    LoginRequest,
    MembershipOut,
    MeResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.auth.security import (
    burn_password_check,
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.common.errors import Conflict, Forbidden, Unauthorized
from app.core.config import Settings
from app.database.session import bind_tenant
from app.organizations.models import Membership, Organization, Role

log = structlog.get_logger(__name__)

INVALID_CREDENTIALS = "INVALID_CREDENTIALS"


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "org"
    return f"{base}-{secrets.token_hex(3)}"


async def _issue_tokens(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    role: str,
    family_id: uuid.UUID | None = None,
) -> tuple[TokenResponse, RefreshToken]:
    raw, token_hash = new_refresh_token()
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        org_id=org_id,
        token_hash=token_hash,
        family_id=family_id or uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
    )
    session.add(row)
    response = TokenResponse(
        access_token=create_access_token(settings, user_id, org_id, role),
        refresh_token=raw,
        expires_in=settings.access_token_ttl_seconds,
        org_id=org_id,
        role=role,
    )
    return response, row


async def _revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_user_tokens_for_org(
    session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID
) -> None:
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.org_id == org_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def register(
    session: AsyncSession, settings: Settings, req: RegisterRequest
) -> TokenResponse:
    """Creates the user, their organization and an ADMIN membership in one transaction."""
    if await session.scalar(select(User.id).where(User.email == req.email)) is not None:
        raise Conflict("Email is already registered", code="EMAIL_ALREADY_REGISTERED")

    user = User(id=uuid.uuid4(), email=req.email, password_hash=await hash_password(req.password))
    session.add(user)
    try:
        # The check above is a fast path; the unique index is what settles concurrent signups.
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("Email is already registered", code="EMAIL_ALREADY_REGISTERED") from exc

    org = Organization(
        id=uuid.uuid4(), name=req.organization_name, slug=_slugify(req.organization_name)
    )
    session.add(org)
    await session.flush()
    session.add(Membership(user_id=user.id, org_id=org.id, role=Role.ADMIN))

    await bind_tenant(session, org.id)
    audit.record(
        session,
        tenant_id=org.id,
        actor_id=user.id,
        action="organization.created",
        target_type="organization",
        target_id=org.id,
    )
    tokens, _ = await _issue_tokens(
        session, settings, user_id=user.id, org_id=org.id, role=Role.ADMIN
    )
    await session.commit()
    log.info("user_registered", user_id=str(user.id), org_id=str(org.id))
    return tokens


async def login(session: AsyncSession, settings: Settings, req: LoginRequest) -> TokenResponse:
    user = await session.scalar(select(User).where(User.email == req.email))
    if user is None:
        await burn_password_check(req.password)
        raise Unauthorized("Invalid email or password", code=INVALID_CREDENTIALS)
    if not await verify_password(user.password_hash, req.password):
        raise Unauthorized("Invalid email or password", code=INVALID_CREDENTIALS)
    if not user.is_active:
        raise Forbidden("Account is disabled", code="ACCOUNT_DISABLED")

    query = select(Membership).where(Membership.user_id == user.id)
    if req.org_id is not None:
        query = query.where(Membership.org_id == req.org_id)
    membership = await session.scalar(query.order_by(Membership.created_at).limit(1))
    if membership is None:
        raise Forbidden("Not a member of this organization", code="NO_MEMBERSHIP")

    tokens, _ = await _issue_tokens(
        session, settings, user_id=user.id, org_id=membership.org_id, role=membership.role
    )
    await session.commit()
    return tokens


async def refresh(session: AsyncSession, settings: Settings, raw_token: str) -> TokenResponse:
    # FOR UPDATE serialises concurrent refreshes of the same token: exactly one rotates it,
    # the other then sees it revoked.
    current = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(raw_token))
        .with_for_update()
    )
    if current is None:
        raise Unauthorized("Invalid refresh token", code="INVALID_REFRESH_TOKEN")

    if current.revoked_at is not None:
        # A rotated token came back: someone else holds a copy. Kill the whole session family.
        await _revoke_family(session, current.family_id)
        await session.commit()
        log.warning("refresh_token_reuse_detected", family_id=str(current.family_id))
        raise Unauthorized("Refresh token has been revoked", code="REFRESH_TOKEN_REUSED")

    if current.expires_at <= datetime.now(UTC):
        raise Unauthorized("Refresh token has expired", code="INVALID_REFRESH_TOKEN")

    role = await session.scalar(
        select(Membership.role)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.user_id == current.user_id,
            Membership.org_id == current.org_id,
            User.is_active.is_(True),
        )
    )
    if role is None:
        await _revoke_family(session, current.family_id)
        await session.commit()
        raise Unauthorized("Membership is no longer valid", code="INVALID_REFRESH_TOKEN")

    tokens, new_row = await _issue_tokens(
        session,
        settings,
        user_id=current.user_id,
        org_id=current.org_id,
        role=role,
        family_id=current.family_id,
    )
    current.revoked_at = datetime.now(UTC)
    current.replaced_by = new_row.id
    await session.commit()
    return tokens


async def logout(session: AsyncSession, raw_token: str) -> None:
    """Revokes the session (token family). Idempotent and silent about unknown tokens."""
    family_id = await session.scalar(
        select(RefreshToken.family_id).where(
            RefreshToken.token_hash == hash_refresh_token(raw_token)
        )
    )
    if family_id is not None:
        await _revoke_family(session, family_id)
        await session.commit()


async def switch_org(
    session: AsyncSession, settings: Settings, user_id: uuid.UUID, org_id: uuid.UUID
) -> TokenResponse:
    role = await session.scalar(
        select(Membership.role).where(Membership.user_id == user_id, Membership.org_id == org_id)
    )
    if role is None:
        raise Forbidden("Not a member of this organization", code="NO_MEMBERSHIP")
    tokens, _ = await _issue_tokens(session, settings, user_id=user_id, org_id=org_id, role=role)
    await session.commit()
    return tokens


async def me(session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> MeResponse:
    user = await session.get_one(User, user_id)
    rows = await session.execute(
        select(Membership.org_id, Organization.name, Membership.role)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user_id)
        .order_by(Membership.created_at)
    )
    return MeResponse(
        user=UserOut.model_validate(user),
        active_org_id=org_id,
        role=role,
        memberships=[MembershipOut(org_id=o, org_name=n, role=r) for o, n, r in rows],
    )
