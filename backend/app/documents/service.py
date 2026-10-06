import uuid
from datetime import UTC, datetime

import structlog
from anyio import to_thread
from fastapi import UploadFile
from sqlalchemy import Select, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.common.errors import Conflict, NotFound, PayloadTooLarge
from app.common.pagination import Page, decode_cursor, encode_cursor
from app.core.config import Settings
from app.documents.models import Document, DocumentStatus
from app.documents.schemas import DocumentOut, SortOrder
from app.documents.storage import StorageBackend
from app.documents.validation import detect_file_type, digest_file, sanitize_filename

log = structlog.get_logger(__name__)


def _not_found() -> NotFound:
    return NotFound("Document does not exist", code="DOCUMENT_NOT_FOUND")


def _active(tenant_id: uuid.UUID) -> Select[Document]:
    return select(Document).where(Document.tenant_id == tenant_id, Document.deleted_at.is_(None))


async def _find_duplicate(
    session: AsyncSession, tenant_id: uuid.UUID, checksum: str
) -> uuid.UUID | None:
    return await session.scalar(
        select(Document.id).where(
            Document.tenant_id == tenant_id,
            Document.checksum == checksum,
            Document.deleted_at.is_(None),
        )
    )


def _duplicate_error(existing_id: uuid.UUID | None) -> Conflict:
    return Conflict(
        "An identical document already exists",
        code="DUPLICATE_DOCUMENT",
        details={"existing_document_id": str(existing_id) if existing_id else None},
    )


async def upload(
    session: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
    principal: Principal,
    file: UploadFile,
) -> Document:
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise PayloadTooLarge(details={"max_bytes": settings.max_upload_bytes})

    digest = await to_thread.run_sync(digest_file, file.file, settings.max_upload_bytes)
    file_type = await to_thread.run_sync(detect_file_type, digest.head, file.file)

    # Fast path. The unique index below is what actually guarantees no duplicates.
    if existing := await _find_duplicate(session, principal.org_id, digest.sha256):
        raise _duplicate_error(existing)

    doc_id = uuid.uuid4()
    storage_key = f"{principal.org_id}/documents/{doc_id}/original.{file_type.extension}"

    # Object first, row second: a crash in between leaves an orphan object (cleaned up by a
    # janitor in Phase 2), never a row pointing at a missing file.
    file.file.seek(0)
    await storage.put(storage_key, file.file)

    document = Document(
        id=doc_id,
        tenant_id=principal.org_id,
        filename=sanitize_filename(file.filename),
        mime_type=file_type.mime_type,
        size_bytes=digest.size_bytes,
        checksum=digest.sha256,
        storage_key=storage_key,
        status=DocumentStatus.QUEUED,
        created_by=principal.user_id,
    )
    session.add(document)
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="document.uploaded",
        target_type="document",
        target_id=doc_id,
        details={"filename": document.filename, "size_bytes": digest.size_bytes},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        # A concurrent upload of the same file won the unique index.
        await session.rollback()
        await storage.delete(storage_key)
        raise _duplicate_error(
            await _find_duplicate(session, principal.org_id, digest.sha256)
        ) from exc

    log.info("document_uploaded", document_id=str(doc_id), size_bytes=digest.size_bytes)
    return document


async def list_documents(
    session: AsyncSession,
    principal: Principal,
    *,
    limit: int,
    cursor: str | None,
    status: DocumentStatus | None,
    sort: SortOrder,
) -> Page[DocumentOut]:
    query = _active(principal.org_id)
    if status is not None:
        query = query.where(Document.status == status)

    position = tuple_(Document.created_at, Document.id)
    if sort is SortOrder.NEWEST:
        query = query.order_by(Document.created_at.desc(), Document.id.desc())
        if cursor:
            query = query.where(position < tuple_(*decode_cursor(cursor)))
    else:
        query = query.order_by(Document.created_at.asc(), Document.id.asc())
        if cursor:
            query = query.where(position > tuple_(*decode_cursor(cursor)))

    rows = list((await session.scalars(query.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return Page(
        items=[DocumentOut.model_validate(r) for r in rows],
        next_cursor=encode_cursor(rows[-1].created_at, rows[-1].id) if has_more else None,
    )


async def get_document(session: AsyncSession, principal: Principal, doc_id: uuid.UUID) -> Document:
    document = await session.scalar(_active(principal.org_id).where(Document.id == doc_id))
    if document is None:
        raise _not_found()
    return document


async def rename_document(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID, filename: str, version: int
) -> Document:
    """Optimistic locking: the UPDATE only matches if nobody changed the row since the client
    read it. No row locks are held while the user is thinking."""
    new_name = sanitize_filename(filename)
    updated: Document | None = await session.scalar(
        update(Document)
        .where(
            Document.id == doc_id,
            Document.tenant_id == principal.org_id,
            Document.deleted_at.is_(None),
            Document.version == version,
        )
        .values(filename=new_name, version=Document.version + 1, updated_at=func.now())
        .returning(Document)
    )
    if updated is None:
        current = await get_document(session, principal, doc_id)  # raises 404 if missing
        raise Conflict(
            "Document was modified by someone else",
            code="VERSION_CONFLICT",
            details={"current_version": current.version},
        )
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="document.renamed",
        target_type="document",
        target_id=doc_id,
        details={"filename": new_name},
    )
    await session.commit()
    return updated


async def delete_document(session: AsyncSession, principal: Principal, doc_id: uuid.UUID) -> None:
    """Soft delete. The stored object is kept; Phase 2 adds retention-based purging."""
    deleted_id = await session.scalar(
        update(Document)
        .where(
            Document.id == doc_id,
            Document.tenant_id == principal.org_id,
            Document.deleted_at.is_(None),
        )
        .values(deleted_at=datetime.now(UTC), version=Document.version + 1, updated_at=func.now())
        .returning(Document.id)
    )
    if deleted_id is None:
        raise _not_found()
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="document.deleted",
        target_type="document",
        target_id=doc_id,
    )
    await session.commit()
