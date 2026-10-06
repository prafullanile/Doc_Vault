"""Engine/session setup and the tenant context that drives PostgreSQL row-level security.

Tenant-owned tables have an RLS policy comparing ``tenant_id`` with the transaction-local
setting ``app.tenant_id``. A session bound to a tenant sets it at the start of every
transaction, so isolation holds even if a query forgets its ``WHERE tenant_id = ...``.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import Settings

TENANT_KEY = "tenant_id"
_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        pool_pre_ping=True,
        echo=settings.database_echo,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, _tx: SessionTransaction, conn: Connection) -> None:
    tenant_id = session.info.get(TENANT_KEY)
    if tenant_id is not None:
        # is_local=true: the setting dies with the transaction, so pooled connections never
        # carry a stale tenant into the next request.
        conn.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})


async def bind_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Scope this session to a tenant: the current transaction (if any) and all later ones."""
    session.info[TENANT_KEY] = tenant_id
    if session.in_transaction():
        await session.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services commit explicitly; anything uncommitted rolls back."""
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


async def ping(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        result: Any = await conn.execute(select(1))
        return bool(result.scalar_one() == 1)
