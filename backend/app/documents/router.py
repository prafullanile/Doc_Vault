import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.auth.dependencies import Admin, Reader, TenantSession, Writer
from app.common.pagination import Page
from app.core.dependencies import AppSettings
from app.documents import service
from app.documents.models import DocumentStatus
from app.documents.schemas import (
    DocumentOut,
    DocumentStatusOut,
    DocumentUpdate,
    DocumentUploadResponse,
    PagesOut,
    SortOrder,
    VersionOut,
)
from app.documents.storage import StorageBackend

router = APIRouter(prefix="/documents", tags=["documents"])

UPLOAD_DESCRIPTION = "PDF, DOCX, TXT, PNG, JPEG or TIFF"


def get_storage(request: Request) -> StorageBackend:
    storage: StorageBackend = request.app.state.storage
    return storage


Storage = Annotated[StorageBackend, Depends(get_storage)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_document(
    response: Response,
    principal: Writer,
    session: TenantSession,
    storage: Storage,
    settings: AppSettings,
    file: Annotated[UploadFile, File(description=UPLOAD_DESCRIPTION)],
) -> DocumentUploadResponse:
    result = await service.upload(session, storage, settings, principal, file)
    response.headers["Location"] = f"/v1/documents/{result.document_id}"
    return result


@router.get("")
async def list_documents(
    principal: Reader,
    session: TenantSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    sort: SortOrder = SortOrder.NEWEST,
) -> Page[DocumentOut]:
    return await service.list_documents(
        session, principal, limit=limit, cursor=cursor, status=status_filter, sort=sort
    )


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID, principal: Reader, session: TenantSession
) -> DocumentOut:
    return await service.get_document(session, principal, document_id)


@router.patch("/{document_id}")
async def update_document(
    document_id: uuid.UUID, body: DocumentUpdate, principal: Writer, session: TenantSession
) -> DocumentOut:
    return await service.rename_document(
        session, principal, document_id, body.filename, body.version
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, principal: Admin, session: TenantSession
) -> Response:
    await service.delete_document(session, principal, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{document_id}/versions", status_code=status.HTTP_201_CREATED)
async def add_version(
    document_id: uuid.UUID,
    principal: Writer,
    session: TenantSession,
    storage: Storage,
    settings: AppSettings,
    file: Annotated[UploadFile, File(description=UPLOAD_DESCRIPTION)],
) -> DocumentUploadResponse:
    return await service.add_version(session, storage, settings, principal, document_id, file)


@router.get("/{document_id}/versions")
async def list_versions(
    document_id: uuid.UUID, principal: Reader, session: TenantSession
) -> list[VersionOut]:
    return await service.list_versions(session, principal, document_id)


@router.get("/{document_id}/status")
async def get_status(
    document_id: uuid.UUID, principal: Reader, session: TenantSession
) -> DocumentStatusOut:
    return await service.get_status(session, principal, document_id)


@router.post("/{document_id}/process", status_code=status.HTTP_202_ACCEPTED)
async def reprocess(
    document_id: uuid.UUID, principal: Writer, session: TenantSession, settings: AppSettings
) -> DocumentStatusOut:
    return await service.reprocess(session, settings, principal, document_id)


@router.get("/{document_id}/pages")
async def list_pages(
    document_id: uuid.UUID,
    principal: Reader,
    session: TenantSession,
    version_no: Annotated[int | None, Query(ge=1)] = None,
    after: Annotated[int | None, Query(ge=0, description="Return pages after this number")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PagesOut:
    return await service.list_pages(
        session, principal, document_id, version_no=version_no, after=after, limit=limit
    )
