import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from app.auth.dependencies import Admin, Reader, TenantSession, Writer
from app.common.pagination import Page
from app.core.dependencies import AppSettings
from app.documents import service
from app.documents.models import DocumentStatus
from app.documents.schemas import DocumentOut, DocumentUpdate, DocumentUploadResponse, SortOrder
from app.documents.storage import StorageBackend

router = APIRouter(prefix="/documents", tags=["documents"])


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
    file: Annotated[UploadFile, File(description="PDF, DOCX, TXT, PNG, JPEG or TIFF")],
) -> DocumentUploadResponse:
    document = await service.upload(session, storage, settings, principal, file)
    response.headers["Location"] = f"/v1/documents/{document.id}"
    return DocumentUploadResponse(document_id=document.id, status=DocumentStatus(document.status))


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
    return DocumentOut.model_validate(await service.get_document(session, principal, document_id))


@router.patch("/{document_id}")
async def update_document(
    document_id: uuid.UUID, body: DocumentUpdate, principal: Writer, session: TenantSession
) -> DocumentOut:
    document = await service.rename_document(
        session, principal, document_id, body.filename, body.version
    )
    return DocumentOut.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, principal: Admin, session: TenantSession
) -> Response:
    await service.delete_document(session, principal, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
