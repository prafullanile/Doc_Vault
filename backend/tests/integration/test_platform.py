"""Health checks, request IDs, the error envelope and schema/migration drift."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.models import Base


async def test_liveness_and_readiness(client):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"


async def test_request_id_is_generated_and_echoed(client):
    generated = await client.get("/health/live")
    assert generated.headers["X-Request-ID"].startswith("req_")

    echoed = await client.get("/health/live", headers={"X-Request-ID": "trace-abc.123"})
    assert echoed.headers["X-Request-ID"] == "trace-abc.123"

    # Header injection attempts are replaced, not echoed.
    bad = await client.get("/health/live", headers={"X-Request-ID": "x" * 500})
    assert bad.headers["X-Request-ID"].startswith("req_")


async def test_security_headers(client):
    resp = await client.get("/health/live")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"


async def test_unknown_route_uses_error_envelope(client):
    resp = await client.get("/v1/nope", headers={"X-Request-ID": "req-404"})
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {"code": "NOT_FOUND", "message": "Not Found", "request_id": "req-404"}
    }


async def test_validation_error_envelope(client):
    resp = await client.post("/v1/auth/register", json={"email": "not-an-email"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    fields = {d["field"] for d in error["details"]}
    assert {"body.email", "body.password", "body.organization_name"} <= fields


async def test_migrations_match_models(database):
    """Fails if someone changes a model without writing the matching migration."""
    engine = create_async_engine(database["owner_async"])
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn), Base.metadata
                )
            )
    finally:
        await engine.dispose()
    assert diff == []
