import uuid
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.auth.models import User
from app.auth.service import revoke_user_tokens_for_org
from app.common.errors import Conflict, NotFound
from app.organizations.models import Membership, Organization, Role
from app.organizations.schemas import MemberOut, OrganizationOut

# memberships has no RLS (login must read it before any tenant is chosen), so every query in
# this module filters on principal.org_id explicitly.


async def get_current(session: AsyncSession, principal: Principal) -> OrganizationOut:
    org = await session.get_one(Organization, principal.org_id)
    return OrganizationOut(
        id=org.id, name=org.name, slug=org.slug, created_at=org.created_at, role=principal.role
    )


def _member_query(org_id: uuid.UUID) -> Select[uuid.UUID, str, str, datetime]:
    return (
        select(Membership.user_id, User.email, Membership.role, Membership.created_at)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == org_id)
    )


async def list_members(session: AsyncSession, principal: Principal) -> list[MemberOut]:
    rows = await session.execute(_member_query(principal.org_id).order_by(Membership.created_at))
    return [MemberOut(user_id=u, email=e, role=Role(r), joined_at=c) for u, e, r, c in rows]


async def _get_member(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> MemberOut:
    row = (await session.execute(_member_query(org_id).where(Membership.user_id == user_id))).one()
    return MemberOut(user_id=row[0], email=row[1], role=Role(row[2]), joined_at=row[3])


async def add_member(
    session: AsyncSession, principal: Principal, email: str, role: Role
) -> MemberOut:
    user_id = await session.scalar(select(User.id).where(User.email == email))
    if user_id is None:
        raise NotFound("No user with this email", code="USER_NOT_FOUND")
    session.add(Membership(user_id=user_id, org_id=principal.org_id, role=role))
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="member.added",
        target_type="user",
        target_id=user_id,
        details={"role": role},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("User is already a member", code="ALREADY_MEMBER") from exc
    return await _get_member(session, principal.org_id, user_id)


async def _lock_membership_and_admins(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Membership, int]:
    """Locks the target membership and every ADMIN membership of the org.

    Without the lock, two admins demoting each other concurrently would each see "another
    admin remains", both succeed, and leave the org with no admin.
    """
    admins = (
        await session.scalars(
            select(Membership)
            .where(Membership.org_id == org_id, Membership.role == Role.ADMIN)
            .order_by(Membership.user_id)  # consistent lock order avoids deadlocks
            .with_for_update()
        )
    ).all()
    target = await session.scalar(
        select(Membership)
        .where(Membership.org_id == org_id, Membership.user_id == user_id)
        .with_for_update()
    )
    if target is None:
        raise NotFound("Member not found", code="MEMBER_NOT_FOUND")
    return target, len(admins)


def _ensure_admin_remains(target: Membership, admin_count: int) -> None:
    if target.role == Role.ADMIN and admin_count <= 1:
        raise Conflict("An organization must keep at least one admin", code="LAST_ADMIN")


async def update_member_role(
    session: AsyncSession, principal: Principal, user_id: uuid.UUID, role: Role
) -> MemberOut:
    target, admin_count = await _lock_membership_and_admins(session, principal.org_id, user_id)
    if role != Role.ADMIN:
        _ensure_admin_remains(target, admin_count)
    previous = target.role
    target.role = role
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="member.role_changed",
        target_type="user",
        target_id=user_id,
        details={"from": previous, "to": role},
    )
    await session.commit()
    return await _get_member(session, principal.org_id, user_id)


async def remove_member(session: AsyncSession, principal: Principal, user_id: uuid.UUID) -> None:
    target, admin_count = await _lock_membership_and_admins(session, principal.org_id, user_id)
    _ensure_admin_remains(target, admin_count)
    await session.delete(target)
    await revoke_user_tokens_for_org(session, user_id, principal.org_id)
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="member.removed",
        target_type="user",
        target_id=user_id,
    )
    await session.commit()
