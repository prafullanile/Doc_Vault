from fastapi import APIRouter

from app.auth.router import router as auth_router
from app.documents.router import router as documents_router
from app.organizations.router import router as organizations_router
from app.processing.router import router as jobs_router
from app.search.router import router as search_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
router.include_router(organizations_router)
router.include_router(documents_router)
router.include_router(jobs_router)
router.include_router(search_router)
