import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from app.documents.models import Document, DocumentPage, DocumentStatus, DocumentVersion
from app.documents.schemas import (
    DocumentOut,
    DocumentStatusOut,
    DocumentUploadResponse,
    PageOut,
    PagesOut,
    SortOrder,
    VersionOut,
)
from app.documents.storage import StorageBackend
from app.documents.validation import detect_file_type, digest_file, sanitize_filename
from app.processing import queue
from app.processing.models import ACTIVE_STATUSES, JobStatus, JobType, ProcessingJob
from app.processing.schemas import JobOut

log = structlog.get_logger(__name__)


def _not_found() -> NotFound:
    return NotFound("Document does not exist", code="DOCUMENT_NOT_FOUND")


def _with_current_version(tenant_id: uuid.UUID) -> Select[Document, DocumentVersion]:
    return (
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .where(Document.tenant_id == tenant_id, Document.deleted_at.is_(None))
    )


async def _find_duplicate(
    session: AsyncSession, tenant_id: uuid.UUID, checksum: str
) -> uuid.UUID | None:
    return await session.scalar(
        select(DocumentVersion.document_id).where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.checksum == checksum,
            DocumentVersion.deleted_at.is_(None),
        )
    )


def _duplicate_error(existing_id: uuid.UUID | None) -> Conflict:
    return Conflict(
        "An identical document already exists",
        code="DUPLICATE_DOCUMENT",
        details={"existing_document_id": str(existing_id) if existing_id else None},
    )


# --- upload ------------------------------------------------------------------------------------


@dataclass
class _StoredFile:
    version_id: uuid.UUID
    storage_key: str
    filename: str
    mime_type: str
    size_bytes: int
    checksum: str


async def _validate_and_store(
    session: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
    principal: Principal,
    document_id: uuid.UUID,
    file: UploadFile,
) -> _StoredFile:
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise PayloadTooLarge(details={"max_bytes": settings.max_upload_bytes})

    digest = await to_thread.run_sync(digest_file, file.file, settings.max_upload_bytes)
    file_type = await to_thread.run_sync(detect_file_type, digest.head, file.file)

    # Fast path. The unique index on versions is what actually guarantees no duplicates.
    if existing := await _find_duplicate(session, principal.org_id, digest.sha256):
        raise _duplicate_error(existing)

    version_id = uuid.uuid4()
    key = (
        f"{principal.org_id}/documents/{document_id}/versions/{version_id}"
        f"/original.{file_type.extension}"
    )
    # Object first, row second: a crash in between leaves an orphan object (removable by a
    # cleanup job), never a row pointing at a missing file.
    file.file.seek(0)
    await storage.put(key, file.file)
    return _StoredFile(
        version_id=version_id,
        storage_key=key,
        filename=sanitize_filename(file.filename),
        mime_type=file_type.mime_type,
        size_bytes=digest.size_bytes,
        checksum=digest.sha256,
    )


def _new_version(
    principal: Principal, document_id: uuid.UUID, version_no: int, stored: _StoredFile
) -> DocumentVersion:
    return DocumentVersion(
        id=stored.version_id,
        tenant_id=principal.org_id,
        document_id=document_id,
        version_no=version_no,
        filename=stored.filename,
        mime_type=stored.mime_type,
        size_bytes=stored.size_bytes,
        checksum=stored.checksum,
        storage_key=stored.storage_key,
        status=DocumentStatus.QUEUED,
        created_by=principal.user_id,
    )


@asynccontextmanager
async def _duplicate_guard(
    session: AsyncSession, storage: StorageBackend, principal: Principal, stored: _StoredFile
) -> AsyncIterator[None]:
    """Wraps every write of a new version. The unique index can fire at any flush, not just
    at commit, so the whole block is covered."""
    try:
        yield
    except IntegrityError as exc:
        # A concurrent upload of the same file won the unique index.
        await session.rollback()
        await storage.delete(stored.storage_key)
        raise _duplicate_error(
            await _find_duplicate(session, principal.org_id, stored.checksum)
        ) from exc


async def upload(
    session: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
    principal: Principal,
    file: UploadFile,
) -> DocumentUploadResponse:
    """Creates the document, version 1 and the first processing job in one transaction, then
    returns immediately. All expensive work happens in the worker."""
    doc_id = uuid.uuid4()
    stored = await _validate_and_store(session, storage, settings, principal, doc_id, file)

    document = Document(
        id=doc_id,
        tenant_id=principal.org_id,
        filename=stored.filename,
        status=DocumentStatus.QUEUED,
        created_by=principal.user_id,
    )
    async with _duplicate_guard(session, storage, principal, stored):
        session.add(document)
        await session.flush()  # documents ↔ versions reference each other: insert in order
        session.add(_new_version(principal, doc_id, 1, stored))
        await session.flush()
        document.current_version_id = stored.version_id
        job = queue.enqueue(
            session,
            tenant_id=principal.org_id,
            document_id=doc_id,
            version_id=stored.version_id,
            job_type=JobType.EXTRACT_TEXT,
            max_attempts=settings.job_max_attempts,
        )
        audit.record(
            session,
            tenant_id=principal.org_id,
            actor_id=principal.user_id,
            action="document.uploaded",
            target_type="document",
            target_id=doc_id,
            details={"filename": stored.filename, "size_bytes": stored.size_bytes},
        )
        await session.commit()
    log.info("document_uploaded", document_id=str(doc_id), size_bytes=stored.size_bytes)
    return DocumentUploadResponse(
        document_id=doc_id,
        version_id=stored.version_id,
        version_no=1,
        job_id=job.id,
        status=DocumentStatus.QUEUED,
    )


async def add_version(
    session: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
    principal: Principal,
    doc_id: uuid.UUID,
    file: UploadFile,
) -> DocumentUploadResponse:
    await _get_document_row(session, principal, doc_id)  # 404 before doing any work
    stored = await _validate_and_store(session, storage, settings, principal, doc_id, file)

    async with _duplicate_guard(session, storage, principal, stored):
        # Lock the document so concurrent uploads of new versions get distinct version numbers.
        document = await _get_document_row(session, principal, doc_id, for_update=True)
        last_no = await session.scalar(
            select(func.max(DocumentVersion.version_no)).where(
                DocumentVersion.document_id == doc_id
            )
        )
        version_no = (last_no or 0) + 1
        await _cancel_active_jobs(session, principal, doc_id)  # older versions are superseded
        session.add(_new_version(principal, doc_id, version_no, stored))
        await session.flush()
        document.current_version_id = stored.version_id
        document.status = DocumentStatus.QUEUED
        document.version += 1
        job = queue.enqueue(
            session,
            tenant_id=principal.org_id,
            document_id=doc_id,
            version_id=stored.version_id,
            job_type=JobType.EXTRACT_TEXT,
            max_attempts=settings.job_max_attempts,
        )
        audit.record(
            session,
            tenant_id=principal.org_id,
            actor_id=principal.user_id,
            action="document.version_added",
            target_type="document",
            target_id=doc_id,
            details={"version_no": version_no, "size_bytes": stored.size_bytes},
        )
        await session.commit()
    return DocumentUploadResponse(
        document_id=doc_id,
        version_id=stored.version_id,
        version_no=version_no,
        job_id=job.id,
        status=DocumentStatus.QUEUED,
    )


# --- reads -------------------------------------------------------------------------------------


async def _get_document_row(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID, *, for_update: bool = False
) -> Document:
    query = select(Document).where(
        Document.id == doc_id,
        Document.tenant_id == principal.org_id,
        Document.deleted_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    document = await session.scalar(query)
    if document is None:
        raise _not_found()
    return document


async def _get_with_version(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID
) -> tuple[Document, DocumentVersion]:
    row = (
        await session.execute(_with_current_version(principal.org_id).where(Document.id == doc_id))
    ).first()
    if row is None:
        raise _not_found()
    return row[0], row[1]


async def get_document(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID
) -> DocumentOut:
    return DocumentOut.build(*await _get_with_version(session, principal, doc_id))


async def list_documents(
    session: AsyncSession,
    principal: Principal,
    *,
    limit: int,
    cursor: str | None,
    status: DocumentStatus | None,
    sort: SortOrder,
) -> Page[DocumentOut]:
    query = _with_current_version(principal.org_id)
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

    rows = (await session.execute(query.limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return Page(
        items=[DocumentOut.build(doc, ver) for doc, ver in rows],
        next_cursor=encode_cursor(last.created_at, last.id) if has_more and last else None,
    )


async def list_versions(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID
) -> list[VersionOut]:
    await _get_document_row(session, principal, doc_id)
    versions = await session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == doc_id, DocumentVersion.tenant_id == principal.org_id)
        .order_by(DocumentVersion.version_no)
    )
    return [VersionOut.model_validate(v) for v in versions]


async def get_status(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID
) -> DocumentStatusOut:
    document, version = await _get_with_version(session, principal, doc_id)
    jobs = await session.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.tenant_id == principal.org_id,
            ProcessingJob.document_version_id == version.id,
        )
        .order_by(ProcessingJob.created_at)
    )
    return DocumentStatusOut(
        document_id=document.id,
        status=DocumentStatus(document.status),
        current_version=VersionOut.model_validate(version),
        jobs=[JobOut.model_validate(j) for j in jobs],
    )


async def list_pages(
    session: AsyncSession,
    principal: Principal,
    doc_id: uuid.UUID,
    *,
    version_no: int | None,
    after: int | None,
    limit: int,
) -> PagesOut:
    document, version = await _get_with_version(session, principal, doc_id)
    if version_no is not None and version_no != version.version_no:
        found = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.tenant_id == principal.org_id,
                DocumentVersion.version_no == version_no,
            )
        )
        if found is None:
            raise NotFound("Version does not exist", code="VERSION_NOT_FOUND")
        version = found

    query = (
        select(DocumentPage)
        .where(DocumentPage.version_id == version.id, DocumentPage.tenant_id == principal.org_id)
        .order_by(DocumentPage.page_number)
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(DocumentPage.page_number > after)
    pages = list((await session.scalars(query)).all())
    has_more = len(pages) > limit
    pages = pages[:limit]
    return PagesOut(
        version_no=version.version_no,
        items=[PageOut.model_validate(p) for p in pages],
        next_cursor=pages[-1].page_number if has_more else None,
    )


# --- processing control ------------------------------------------------------------------------


async def _cancel_active_jobs(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID
) -> None:
    """Queued jobs are cancelled outright; running ones are flagged, and their worker stops
    at the next heartbeat."""
    scope = (ProcessingJob.tenant_id == principal.org_id, ProcessingJob.document_id == doc_id)
    await session.execute(
        update(ProcessingJob)
        .where(*scope, ProcessingJob.status.in_((JobStatus.QUEUED, JobStatus.RETRYING)))
        .values(status=JobStatus.CANCELLED, completed_at=func.now(), updated_at=func.now())
    )
    await session.execute(
        update(ProcessingJob)
        .where(*scope, ProcessingJob.status == JobStatus.RUNNING)
        .values(cancel_requested=True, updated_at=func.now())
    )


async def reprocess(
    session: AsyncSession, settings: Settings, principal: Principal, doc_id: uuid.UUID
) -> DocumentStatusOut:
    document = await _get_document_row(session, principal, doc_id, for_update=True)
    if document.current_version_id is None:  # cannot happen: set in the creating transaction
        raise Conflict("Document has no version", code="NO_VERSION")
    active = await session.scalar(
        select(ProcessingJob.id).where(
            ProcessingJob.tenant_id == principal.org_id,
            ProcessingJob.document_version_id == document.current_version_id,
            ProcessingJob.status.in_(ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise Conflict("Document is already being processed", code="PROCESSING_IN_PROGRESS")

    queue.enqueue(
        session,
        tenant_id=principal.org_id,
        document_id=doc_id,
        version_id=document.current_version_id,
        job_type=JobType.EXTRACT_TEXT,
        max_attempts=settings.job_max_attempts,
    )
    await queue.update_version_status(
        session,
        tenant_id=principal.org_id,
        document_id=doc_id,
        version_id=document.current_version_id,
        status=DocumentStatus.QUEUED,
    )
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="document.reprocess_requested",
        target_type="document",
        target_id=doc_id,
    )
    try:
        await session.commit()
    except IntegrityError as exc:  # a concurrent reprocess won the unique active-job index
        await session.rollback()
        raise Conflict(
            "Document is already being processed", code="PROCESSING_IN_PROGRESS"
        ) from exc
    return await get_status(session, principal, doc_id)


# --- writes ------------------------------------------------------------------------------------


async def rename_document(
    session: AsyncSession, principal: Principal, doc_id: uuid.UUID, filename: str, version: int
) -> DocumentOut:
    """Optimistic locking: the UPDATE only matches if nobody changed the row since the client
    read it. No row locks are held while the user is thinking."""
    new_name = sanitize_filename(filename)
    updated = await session.scalar(
        update(Document)
        .where(
            Document.id == doc_id,
            Document.tenant_id == principal.org_id,
            Document.deleted_at.is_(None),
            Document.version == version,
        )
        .values(filename=new_name, version=Document.version + 1, updated_at=func.now())
        .returning(Document.id)
    )
    if updated is None:
        current = await _get_document_row(session, principal, doc_id)  # raises 404 if missing
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
    return await get_document(session, principal, doc_id)


async def delete_document(session: AsyncSession, principal: Principal, doc_id: uuid.UUID) -> None:
    """Soft delete of the document and its versions; pending processing is cancelled.
    Stored objects are kept (retention-based purging comes later)."""
    now = datetime.now(UTC)
    deleted_id = await session.scalar(
        update(Document)
        .where(
            Document.id == doc_id,
            Document.tenant_id == principal.org_id,
            Document.deleted_at.is_(None),
        )
        .values(deleted_at=now, version=Document.version + 1, updated_at=func.now())
        .returning(Document.id)
    )
    if deleted_id is None:
        raise _not_found()
    await session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.document_id == doc_id, DocumentVersion.tenant_id == principal.org_id)
        .values(deleted_at=now)
    )
    await _cancel_active_jobs(session, principal, doc_id)
    audit.record(
        session,
        tenant_id=principal.org_id,
        actor_id=principal.user_id,
        action="document.deleted",
        target_type="document",
        target_id=doc_id,
    )
    await session.commit()
