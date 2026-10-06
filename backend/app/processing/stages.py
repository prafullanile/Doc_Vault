"""Pipeline stages. Each stage is one job type and is retried independently (§15).

A stage has two halves:
- ``execute``: the slow part (download, extract, detect). No database writes, so a failure
  or a lost lease leaves nothing half-written.
- ``persist``: writes the results inside the job's completion transaction, together with
  marking the job done and enqueueing the next stage, all atomically. Outputs are replaced,
  never appended, so re-running a stage is idempotent.
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langdetect import DetectorFactory, LangDetectException, detect
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.database.session import bind_tenant
from app.documents.models import DocumentPage, DocumentVersion
from app.documents.storage import ObjectNotFound, StorageBackend
from app.processing.errors import PermanentError
from app.processing.extraction.runner import run_extraction
from app.processing.extraction.types import ExtractionResult
from app.processing.models import JobType
from app.processing.queue import ClaimedJob

PIPELINE_VERSION = "2026.10-p2"
PIPELINE: tuple[JobType, ...] = (JobType.EXTRACT_TEXT, JobType.DETECT_LANGUAGE)

DetectorFactory.seed = 0  # langdetect is randomised; make it deterministic
LANGUAGE_SAMPLE_CHARS = 20_000


def next_stage(job_type: JobType) -> JobType | None:
    index = PIPELINE.index(job_type)
    return PIPELINE[index + 1] if index + 1 < len(PIPELINE) else None


@dataclass
class StageContext:
    job: ClaimedJob
    settings: Settings
    storage: StorageBackend
    sessionmaker: async_sessionmaker[AsyncSession]


class Stage(Protocol):
    job_type: JobType

    async def execute(self, ctx: StageContext) -> Any: ...

    async def persist(self, session: AsyncSession, ctx: StageContext, result: Any) -> None: ...


async def _load_version(ctx: StageContext) -> DocumentVersion:
    async with ctx.sessionmaker() as session:
        await bind_tenant(session, ctx.job.tenant_id)
        version = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == ctx.job.version_id,
                DocumentVersion.deleted_at.is_(None),
            )
        )
    if version is None:
        raise PermanentError("DOCUMENT_DELETED", "The document version no longer exists")
    return version


class ExtractTextStage:
    job_type = JobType.EXTRACT_TEXT

    async def execute(self, ctx: StageContext) -> ExtractionResult:
        version = await _load_version(ctx)
        with tempfile.TemporaryDirectory(prefix="docunexus-") as tmp:
            workdir = Path(tmp)
            source = workdir / "source"
            try:
                await ctx.storage.download(version.storage_key, source)
            except ObjectNotFound as exc:
                raise PermanentError("FILE_MISSING", "The stored file was not found") from exc
            # Any other StorageError propagates and is retried (storage outage).
            return await run_extraction(source, version.mime_type, ctx.settings, workdir)

    async def persist(
        self, session: AsyncSession, ctx: StageContext, result: ExtractionResult
    ) -> None:
        job = ctx.job
        await session.execute(delete(DocumentPage).where(DocumentPage.version_id == job.version_id))
        if result.pages:
            await session.execute(
                insert(DocumentPage),
                [
                    {
                        "version_id": job.version_id,
                        "tenant_id": job.tenant_id,
                        "page_number": page.page_number,
                        "section": page.section,
                        "text": page.text,
                        "source": page.source,
                        "ocr_confidence": page.ocr_confidence,
                        "char_count": len(page.text),
                    }
                    for page in result.pages
                ],
            )
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == job.version_id)
            .values(page_count=result.page_count, ocr_page_count=result.ocr_page_count)
        )


class DetectLanguageStage:
    job_type = JobType.DETECT_LANGUAGE

    async def execute(self, ctx: StageContext) -> str | None:
        async with ctx.sessionmaker() as session:
            await bind_tenant(session, ctx.job.tenant_id)
            texts = (
                await session.scalars(
                    select(DocumentPage.text)
                    .where(DocumentPage.version_id == ctx.job.version_id)
                    .order_by(DocumentPage.page_number)
                    .limit(50)
                )
            ).all()
        sample = "\n".join(texts)[:LANGUAGE_SAMPLE_CHARS]
        if len(sample.strip()) < 20:
            return None
        try:
            return str(detect(sample))
        except LangDetectException:
            return None

    async def persist(self, session: AsyncSession, ctx: StageContext, result: str | None) -> None:
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == ctx.job.version_id)
            .values(language=result)
        )


STAGES: dict[JobType, Stage] = {
    JobType.EXTRACT_TEXT: ExtractTextStage(),
    JobType.DETECT_LANGUAGE: DetectLanguageStage(),
}
