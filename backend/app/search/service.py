"""Hybrid search: full-text and vector retrieval, fused with RRF, then reranked (§23-24).

    query ─┬─► keyword (tsvector @@ websearch_to_tsquery) ─┐
           └─► vector  (HNSW, cosine)  ────────────────────┴─► RRF ─► cross-encoder ─► top k

Only current versions of non-deleted documents are searched. Every query is tenant-scoped
twice: an explicit `tenant_id` filter, and RLS on the tenant-bound session.

Degradation: if the embedding model is unavailable, hybrid search falls back to keyword-only;
if the reranker fails, the fused order is kept. Both are reported in `warnings`.
"""

import time
import uuid
from typing import Any

import structlog
from anyio import to_thread
from sqlalchemy import ColumnElement, Select, cast, func, select, text
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal
from app.common.errors import AppError
from app.core.config import Settings
from app.documents.models import Document, DocumentVersion
from app.search.embeddings import get_embedder, get_reranker
from app.search.fusion import FusedHit, reciprocal_rank_fusion
from app.search.models import FTS_CONFIG, ChunkEmbedding, DocumentChunk
from app.search.schemas import (
    ScoreBreakdown,
    SearchFilters,
    SearchMode,
    SearchRequest,
    SearchResponse,
    SearchResult,
)

log = structlog.get_logger(__name__)


class SearchUnavailable(AppError):
    status_code = 503
    code = "SEARCH_UNAVAILABLE"
    message = "Semantic search is temporarily unavailable"


def _scope(query: Select[Any], principal: Principal, filters: SearchFilters) -> Select[Any]:
    """Joins a chunk query to its version and document and applies tenant + user filters."""
    conditions: list[ColumnElement[bool]] = [
        DocumentChunk.tenant_id == principal.org_id,
        Document.tenant_id == principal.org_id,
        Document.deleted_at.is_(None),
    ]
    if filters.document_ids:
        conditions.append(Document.id.in_(filters.document_ids))
    if filters.mime_types:
        conditions.append(DocumentVersion.mime_type.in_(filters.mime_types))
    if filters.languages:
        conditions.append(DocumentVersion.language.in_(filters.languages))
    if filters.created_after:
        conditions.append(Document.created_at >= filters.created_after)
    if filters.created_before:
        conditions.append(Document.created_at < filters.created_before)
    return (
        query.join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
        .join(Document, Document.current_version_id == DocumentVersion.id)
        .where(*conditions)
    )


async def _keyword(
    session: AsyncSession, principal: Principal, req: SearchRequest, limit: int
) -> list[uuid.UUID]:
    # websearch syntax: "exact phrase", -excluded, OR — and never a syntax error on user input.
    tsquery = func.websearch_to_tsquery(cast(FTS_CONFIG, REGCONFIG), req.query)
    rank = func.ts_rank_cd(DocumentChunk.search_vector, tsquery).label("rank")
    query = _scope(select(DocumentChunk.id, rank), principal, req.filters).where(
        DocumentChunk.search_vector.op("@@")(tsquery)
    )
    rows = await session.execute(query.order_by(rank.desc(), DocumentChunk.id).limit(limit))
    return [chunk_id for chunk_id, _ in rows]


async def _vector(
    session: AsyncSession,
    principal: Principal,
    req: SearchRequest,
    query_vector: list[float],
    model: str,
    limit: int,
) -> list[uuid.UUID]:
    # HNSW applies WHERE clauses *after* walking the graph, so a small tenant in a large index
    # could get too few rows back. Iterative scan keeps walking until the LIMIT is satisfied.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {max(40, limit * 2)}"))
    distance = ChunkEmbedding.embedding.cosine_distance(query_vector).label("distance")
    query = _scope(
        select(ChunkEmbedding.chunk_id, distance).join(
            DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id
        ),
        principal,
        req.filters,
    ).where(ChunkEmbedding.model == model, ChunkEmbedding.tenant_id == principal.org_id)
    rows = (await session.execute(query.order_by(distance).limit(limit))).all()
    # relaxed_order may return slightly out-of-order rows; restore exact order.
    return [chunk_id for chunk_id, _ in sorted(rows, key=lambda r: (r[1], str(r[0])))]


async def _details(
    session: AsyncSession, principal: Principal, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not chunk_ids:
        return {}
    rows = await session.execute(
        select(
            DocumentChunk.id,
            DocumentChunk.text,
            DocumentChunk.page_number,
            DocumentChunk.section,
            Document.id.label("document_id"),
            Document.filename,
            DocumentVersion.version_no,
        )
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentChunk.id.in_(chunk_ids), DocumentChunk.tenant_id == principal.org_id)
    )
    return {row.id: row for row in rows}


async def search(
    session: AsyncSession, settings: Settings, principal: Principal, req: SearchRequest
) -> SearchResponse:
    timings: dict[str, float] = {}
    warnings: list[str] = []
    started = time.perf_counter()

    def lap(name: str, since: float) -> float:
        now = time.perf_counter()
        timings[name] = round((now - since) * 1000, 2)
        return now

    ranked: dict[str, list[uuid.UUID]] = {}
    mark = started
    if req.mode in (SearchMode.HYBRID, SearchMode.KEYWORD):
        ranked["keyword"] = await _keyword(session, principal, req, settings.search_candidates)
        mark = lap("keyword", mark)

    if req.mode in (SearchMode.HYBRID, SearchMode.VECTOR):
        try:
            embedder = await to_thread.run_sync(get_embedder, settings)
            query_vector = await to_thread.run_sync(embedder.embed_query, req.query)
        except Exception as exc:
            log.error("query_embedding_failed", error=str(exc))
            if req.mode is SearchMode.VECTOR:
                raise SearchUnavailable() from exc
            warnings.append("vector_search_unavailable")
        else:
            mark = lap("embed_query", mark)
            ranked["vector"] = await _vector(
                session,
                principal,
                req,
                query_vector,
                embedder.model_name,
                settings.search_candidates,
            )
            mark = lap("vector", mark)

    fused: list[FusedHit] = reciprocal_rank_fusion(ranked)
    pool = fused[: max(settings.rerank_candidates, req.limit)]
    details = await _details(session, principal, [hit.chunk_id for hit in pool])
    pool = [hit for hit in pool if hit.chunk_id in details]

    rerank_scores: dict[uuid.UUID, float] = {}
    reranker = get_reranker(settings) if req.rerank and pool else None
    if reranker is not None:
        try:
            scores = await to_thread.run_sync(
                reranker.score, req.query, [details[h.chunk_id].text for h in pool]
            )
            rerank_scores = {h.chunk_id: s for h, s in zip(pool, scores, strict=True)}
            pool.sort(key=lambda h: -rerank_scores[h.chunk_id])
            mark = lap("rerank", mark)
        except Exception as exc:  # keep the fused order rather than failing the search
            log.error("rerank_failed", error=str(exc))
            warnings.append("rerank_unavailable")

    results = []
    for hit in pool[: req.limit]:
        row = details[hit.chunk_id]
        rerank = rerank_scores.get(hit.chunk_id)
        results.append(
            SearchResult(
                chunk_id=hit.chunk_id,
                document_id=row.document_id,
                filename=row.filename,
                version_no=row.version_no,
                page_number=row.page_number,
                section=row.section,
                text=row.text,
                score=rerank if rerank is not None else round(hit.score, 6),
                scores=ScoreBreakdown(
                    keyword_rank=hit.ranks.get("keyword"),
                    vector_rank=hit.ranks.get("vector"),
                    fused=round(hit.score, 6),
                    rerank=rerank,
                ),
            )
        )
    lap("total", started)
    return SearchResponse(
        query=req.query,
        mode=req.mode,
        reranked=bool(rerank_scores),
        results=results,
        warnings=warnings,
        timings_ms=timings,
    )
