"""Pipeline stages. Each stage is one job type and is retried independently (§15).

A stage has two halves:
- ``execute``: the slow part (download, extract, detect). No database writes, so a failure
  or a lost lease leaves nothing half-written.
- ``persist``: writes the results inside the job's completion transaction, together with
  marking the job done and enqueueing the next stage, all atomically. Outputs are replaced,
  never appended, so re-running a stage is idempotent.
"""

import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from anyio import to_thread
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
from app.search.chunking import CHUNKER_VERSION, Chunk, PageText, chunk_pages
from app.search.embeddings import get_embedder
from app.search.models import ChunkEmbedding, DocumentChunk

PIPELINE_VERSION = f"2026.10-p4+{CHUNKER_VERSION}"
PIPELINE: tuple[JobType, ...] = (
    JobType.EXTRACT_TEXT,
    JobType.DETECT_LANGUAGE,
    JobType.CHUNK,
    JobType.EMBED,
)
EMBED_SLICE = 64  # chunks per model call; the stage can be cancelled between slices

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


class ChunkStage:
    job_type = JobType.CHUNK

    async def execute(self, ctx: StageContext) -> list[Chunk]:
        async with ctx.sessionmaker() as session:
            await bind_tenant(session, ctx.job.tenant_id)
            rows = (
                await session.execute(
                    select(DocumentPage.page_number, DocumentPage.text, DocumentPage.section)
                    .where(DocumentPage.version_id == ctx.job.version_id)
                    .order_by(DocumentPage.page_number)
                )
            ).all()
        pages = [PageText(number, text, section) for number, text, section in rows]
        return chunk_pages(pages, ctx.settings.chunk_target_words, ctx.settings.chunk_overlap_words)

    async def persist(self, session: AsyncSession, ctx: StageContext, result: list[Chunk]) -> None:
        job = ctx.job
        # Deleting the chunks cascades to their embeddings; EMBED rebuilds them next.
        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.version_id == job.version_id)
        )
        if result:
            await session.execute(
                insert(DocumentChunk),
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": job.tenant_id,
                        "version_id": job.version_id,
                        "position": chunk.position,
                        "page_number": chunk.page_number,
                        "section": chunk.section,
                        "text": chunk.text,
                        "token_count": chunk.token_count,
                    }
                    for chunk in result
                ],
            )
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == job.version_id)
            .values(chunk_count=len(result))
        )


@dataclass
class EmbeddingResult:
    model: str
    vectors: list[tuple[uuid.UUID, list[float]]]


class EmbedStage:
    job_type = JobType.EMBED

    async def execute(self, ctx: StageContext) -> EmbeddingResult:
        async with ctx.sessionmaker() as session:
            await bind_tenant(session, ctx.job.tenant_id)
            rows = (
                await session.execute(
                    select(DocumentChunk.id, DocumentChunk.text)
                    .where(DocumentChunk.version_id == ctx.job.version_id)
                    .order_by(DocumentChunk.position)
                )
            ).all()
        embedder = await to_thread.run_sync(get_embedder, ctx.settings)  # loads once per process
        vectors: list[tuple[uuid.UUID, list[float]]] = []
        for start in range(0, len(rows), EMBED_SLICE):
            batch = rows[start : start + EMBED_SLICE]
            embedded = await to_thread.run_sync(
                embedder.embed_documents, [text for _, text in batch]
            )
            vectors.extend(
                (chunk_id, vec) for (chunk_id, _), vec in zip(batch, embedded, strict=True)
            )
        return EmbeddingResult(model=embedder.model_name, vectors=vectors)

    async def persist(
        self, session: AsyncSession, ctx: StageContext, result: EmbeddingResult
    ) -> None:
        job = ctx.job
        await session.execute(
            delete(ChunkEmbedding).where(
                ChunkEmbedding.version_id == job.version_id, ChunkEmbedding.model == result.model
            )
        )
        if result.vectors:
            await session.execute(
                insert(ChunkEmbedding),
                [
                    {
                        "chunk_id": chunk_id,
                        "model": result.model,
                        "tenant_id": job.tenant_id,
                        "version_id": job.version_id,
                        "embedding": vector,
                    }
                    for chunk_id, vector in result.vectors
                ],
            )


STAGES: dict[JobType, Stage] = {
    JobType.EXTRACT_TEXT: ExtractTextStage(),
    JobType.DETECT_LANGUAGE: DetectLanguageStage(),
    JobType.CHUNK: ChunkStage(),
    JobType.EMBED: EmbedStage(),
}
