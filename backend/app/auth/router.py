from fastapi import APIRouter, Response, status

from app.auth import service
from app.auth.dependencies import CurrentPrincipal
from app.auth.schemas import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    SwitchOrgRequest,
    TokenResponse,
)
from app.core.dependencies import AppSettings, DbSession

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, session: DbSession, settings: AppSettings
) -> TokenResponse:
    return await service.register(session, settings, body)


@router.post("/login")
async def login(body: LoginRequest, session: DbSession, settings: AppSettings) -> TokenResponse:
    return await service.login(session, settings, body)


@router.post("/refresh")
async def refresh(body: RefreshRequest, session: DbSession, settings: AppSettings) -> TokenResponse:
    return await service.refresh(session, settings, body.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, session: DbSession) -> Response:
    await service.logout(session, body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/switch-org")
async def switch_org(
    body: SwitchOrgRequest, principal: CurrentPrincipal, session: DbSession, settings: AppSettings
) -> TokenResponse:
    return await service.switch_org(session, settings, principal.user_id, body.org_id)


@router.get("/me")
async def me(principal: CurrentPrincipal, session: DbSession) -> MeResponse:
    return await service.me(session, principal.user_id, principal.org_id, principal.role)
