import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.database.session import ping

router = APIRouter(prefix="/health", tags=["health"])
log = structlog.get_logger(__name__)


@router.get("/live")
async def live() -> dict[str, str]:
    """The process is up. Never checks dependencies, so a DB outage doesn't restart pods."""
    return {"status": "ok"}


@router.get("/ready", response_model=None)
async def ready(request: Request) -> JSONResponse:
    """Ready to serve traffic: dependencies are reachable."""
    try:
        database_ok = await ping(request.app.state.engine)
    except Exception as exc:  # any failure means "not ready", not a 500
        log.warning("readiness_check_failed", error=str(exc))
        database_ok = False
    checks = {"database": "ok" if database_ok else "unavailable"}
    status = "ok" if database_ok else "unavailable"
    return JSONResponse(
        {"status": status, "checks": checks}, status_code=200 if database_ok else 503
    )
