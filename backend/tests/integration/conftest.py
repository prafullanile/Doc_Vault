"""Integration fixtures: a real PostgreSQL with the real migrations and the real RLS policies.

Point TEST_POSTGRES_URL at a server where the given user can create databases and roles
(e.g. postgresql://postgres:postgres@localhost:5432/postgres). Without it, a throwaway
container is started via testcontainers (needs Docker).
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.main import create_app
from app.worker.worker import Worker

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEST_DB = "docunexus_test"
APP_ROLE = "docunexus_test_app"
APP_PASSWORD = "test-app-password"
WORKER_ROLE = "docunexus_test_worker"
WORKER_PASSWORD = "test-worker-password"
MAX_UPLOAD_BYTES = 1024 * 1024
TABLES = (
    "users, organizations, memberships, refresh_tokens, documents, document_versions, "
    "document_pages, document_chunks, chunk_embeddings, processing_jobs, job_attempts, "
    "queries, query_sources, audit_logs"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.nodeid:
            item.add_marker(pytest.mark.integration)


def _with_db(
    url: str,
    database: str,
    *,
    user: str | None = None,
    password: str | None = None,
    driver: str = "postgresql",
) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((driver, netloc, f"/{database}", "", ""))


@pytest.fixture(scope="session")
def server_url() -> Iterator[str]:
    url = os.environ.get("TEST_POSTGRES_URL")
    if url:
        yield url.replace("postgresql+asyncpg://", "postgresql://")
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("Set TEST_POSTGRES_URL or install testcontainers")
    try:
        container = PostgresContainer("pgvector/pgvector:pg16", driver=None)
        container.start()
    except Exception as exc:
        pytest.skip(f"No TEST_POSTGRES_URL and Docker is unavailable: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture(scope="session")
async def database(server_url: str) -> AsyncIterator[dict[str, str]]:
    admin = await asyncpg.connect(server_url)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {TEST_DB}")
        for role, password in ((APP_ROLE, APP_PASSWORD), (WORKER_ROLE, WORKER_PASSWORD)):
            if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                await admin.execute(
                    f"CREATE ROLE {role} LOGIN PASSWORD '{password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                )
    finally:
        await admin.close()

    owner_url = _with_db(server_url, TEST_DB)
    urls = {
        "owner": owner_url,
        "owner_async": _with_db(server_url, TEST_DB, driver="postgresql+asyncpg"),
        "app": _with_db(server_url, TEST_DB, user=APP_ROLE, password=APP_PASSWORD),
        "app_async": _with_db(
            server_url, TEST_DB, user=APP_ROLE, password=APP_PASSWORD, driver="postgresql+asyncpg"
        ),
        "worker": _with_db(server_url, TEST_DB, user=WORKER_ROLE, password=WORKER_PASSWORD),
        "worker_async": _with_db(
            server_url,
            TEST_DB,
            user=WORKER_ROLE,
            password=WORKER_PASSWORD,
            driver="postgresql+asyncpg",
        ),
    }

    os.environ["DB_APP_ROLE"] = APP_ROLE
    os.environ["DB_WORKER_ROLE"] = WORKER_ROLE
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["url"] = urls["owner_async"]
    # env.py calls asyncio.run(); keep it off this test event loop.
    await asyncio.to_thread(command.upgrade, config, "head")
    yield urls


@pytest.fixture(scope="session")
def settings(database: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        environment="test",
        log_level="WARNING",
        database_url=database["app_async"],
        database_pool_size=5,
        jwt_secret="test-secret-" + "x" * 40,
        storage_local_dir=tmp_path_factory.mktemp("storage"),
        max_upload_bytes=MAX_UPLOAD_BYTES,
        job_max_attempts=3,
        retry_base_seconds=30,
        extraction_timeout_seconds=60,
        # No model downloads in tests: deterministic stand-ins with the same interfaces.
        embedding_backend="hashing",
        reranker_backend="none",
        llm_backend="fake",
    )


@pytest.fixture(scope="session")
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean_tables(database: dict[str, str]) -> AsyncIterator[None]:
    yield
    conn = await asyncpg.connect(database["owner"])
    try:
        await conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@pytest.fixture
async def owner_conn(database: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database["owner"])
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_conn(database: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    """A raw connection as the restricted app role — bypasses the application entirely."""
    conn = await asyncpg.connect(database["app"])
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def worker_conn(database: dict[str, str]) -> AsyncIterator[asyncpg.Connection]:
    """A raw connection as the worker role."""
    conn = await asyncpg.connect(database["worker"])
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def worker_sessionmaker(
    database: dict[str, str],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database["worker_async"], pool_size=10)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def worker(
    app: FastAPI, settings: Settings, worker_sessionmaker: async_sessionmaker[AsyncSession]
) -> Worker:
    """A worker connected as the worker role, sharing the API's storage."""
    return Worker(settings, worker_sessionmaker, app.state.storage, worker_id="test-worker-1")
