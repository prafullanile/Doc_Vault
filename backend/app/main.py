from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import app.database.models  # noqa: F401  (registers every mapper; FKs span all tables)
from app.api import health, v1
from app.common.errors import error_response, register_error_handlers
from app.common.middleware import RequestContextMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database.session import create_engine, create_sessionmaker
from app.documents.storage import StorageError, create_storage

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.storage = await create_storage(settings)
        yield
        await engine.dispose()

    app = FastAPI(
        title="DocuNexus API",
        version="0.2.0",
        description="Distributed AI-powered document intelligence platform",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    @app.exception_handler(StorageError)
    async def _storage_unavailable(_: Request, exc: StorageError) -> JSONResponse:
        log.error("storage_unavailable", error=str(exc))
        return error_response(
            503,
            "STORAGE_UNAVAILABLE",
            "Document storage is temporarily unavailable",
            headers={"Retry-After": "30"},
        )

    app.include_router(health.router)
    app.include_router(v1.router)
    return app
