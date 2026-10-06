"""Alembic environment.

Migrations run as the schema *owner* (MIGRATIONS_DATABASE_URL), separate from the restricted
roles the API and the worker connect as. DB_APP_ROLE and DB_WORKER_ROLE name those roles so
migrations can grant each exactly the privileges it needs.
"""

import asyncio
import os
import re

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.models import Base

config = context.config
target_metadata = Base.metadata


def _role(env_var: str, default: str) -> str:
    role = os.environ.get(env_var, default)
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise RuntimeError(f"Invalid {env_var}: {role!r}")
    return role


config.attributes["app_role"] = _role("DB_APP_ROLE", "docunexus_app")
config.attributes["worker_role"] = _role("DB_WORKER_ROLE", "docunexus_worker")


def _url() -> str:
    url = config.attributes.get("url") or os.environ.get("MIGRATIONS_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATIONS_DATABASE_URL is not set")
    return str(url)


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_async())
