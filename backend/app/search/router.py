from fastapi import APIRouter

from app.auth.dependencies import Reader, TenantSession
from app.core.dependencies import AppSettings
from app.search import service
from app.search.schemas import SearchRequest, SearchResponse

router = APIRouter(prefix="/search", tags=["search"])


@router.post("")
async def search(
    body: SearchRequest, principal: Reader, session: TenantSession, settings: AppSettings
) -> SearchResponse:
    return await service.search(session, settings, principal, body)
