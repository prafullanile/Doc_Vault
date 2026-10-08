"""Retrieval-augmented question answering (§25-27).

    question ─► hybrid search (Phase 4) ─► drop weak sources ─► numbered context
             ─► LLM (answer only from sources, cite [n]) ─► citation check ─► persisted answer

Guarantees:
- No relevant sources: no LLM call, and a fixed "not found" answer. Nothing to hallucinate from.
- Citations can only point at sources that were actually retrieved and given to the model.
- LLM down or not configured: the sources are still returned (status LLM_UNAVAILABLE).
- Every question, answer and source list is stored per tenant, so answers can be audited later.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.dependencies import Principal
from app.common.errors import NotFound
from app.common.pagination import decode_cursor, encode_cursor
from app.core.config import Settings
from app.database.session import bind_tenant
from app.rag.llm import ChatMessage, ChatModel, Completion, LLMUnavailable
from app.rag.models import Query, QuerySource, QueryStatus
from app.rag.prompt import HistoryTurn, PromptSource, build_messages, check_citations
from app.rag.schemas import (
    QueryHistory,
    QueryRequest,
    QueryResponse,
    QuerySummary,
    SourceOut,
    Usage,
)
from app.search import service as search_service
from app.search.schemas import SearchMode, SearchRequest

log = structlog.get_logger(__name__)

NOT_FOUND_ANSWER = "I could not find anything relevant to this question in your documents."


@dataclass
class Prepared:
    query_id: uuid.UUID
    conversation_id: uuid.UUID
    question: str
    sources: list[SourceOut]
    messages: list[ChatMessage]
    timings: dict[str, float] = field(default_factory=dict)


async def _history(
    session: AsyncSession, principal: Principal, conversation_id: uuid.UUID, turns: int
) -> list[HistoryTurn]:
    rows = (
        await session.execute(
            select(Query.question, Query.answer)
            .where(
                Query.tenant_id == principal.org_id,
                Query.user_id == principal.user_id,
                Query.conversation_id == conversation_id,
                Query.status == QueryStatus.ANSWERED,
            )
            .order_by(Query.created_at.desc())
            .limit(turns)
        )
    ).all()
    return [HistoryTurn(q, a or "") for q, a in reversed(rows)]


async def prepare(
    session: AsyncSession, settings: Settings, principal: Principal, req: QueryRequest
) -> Prepared:
    started = time.perf_counter()
    history: list[HistoryTurn] = []
    if req.conversation_id is not None:
        history = await _history(
            session, principal, req.conversation_id, settings.rag_history_turns
        )
    # A follow-up ("and in 2024?") rarely retrieves well on its own: add the previous question.
    retrieval_query = f"{history[-1].question} {req.question}" if history else req.question

    found = await search_service.search(
        session,
        settings,
        principal,
        SearchRequest(
            query=retrieval_query[:1000],
            mode=SearchMode.HYBRID,
            limit=settings.rag_context_chunks,
            rerank=True,
            filters=req.filters,
        ),
    )
    results = [
        r
        for r in found.results
        if not (
            found.reranked
            and r.scores.rerank is not None
            and r.scores.rerank < settings.rag_min_rerank_score
        )
    ]
    candidates = [
        SourceOut(
            number=i,
            document_id=r.document_id,
            filename=r.filename,
            version_no=r.version_no,
            page_number=r.page_number,
            chunk_id=r.chunk_id,
            score=r.score,
            excerpt=r.text,
            cited=False,
        )
        for i, r in enumerate(results, start=1)
    ]
    messages, included = build_messages(
        req.question,
        [PromptSource(s.number, s.filename, s.page_number, s.excerpt) for s in candidates],
        history,
        settings.rag_max_context_chars,
    )
    return Prepared(
        query_id=uuid.uuid4(),
        conversation_id=req.conversation_id or uuid.uuid4(),
        question=req.question,
        sources=[s for s in candidates if s.number in included],
        messages=messages,
        timings={"retrieval": round((time.perf_counter() - started) * 1000, 2)},
    )


async def finalize(
    session: AsyncSession,
    principal: Principal,
    prepared: Prepared,
    *,
    status: QueryStatus,
    completion: Completion | None,
) -> QueryResponse:
    """Checks citations, stores the query with its sources, and builds the response."""
    answer: str | None
    cited: list[int]
    if status is QueryStatus.NO_RELEVANT_DOCUMENTS:
        answer, cited = NOT_FOUND_ANSWER, []
    elif completion is not None:
        answer, cited = check_citations(completion.text, {s.number for s in prepared.sources})
    else:
        answer, cited = None, []
    sources = [s.model_copy(update={"cited": s.number in cited}) for s in prepared.sources]

    query = Query(
        id=prepared.query_id,
        tenant_id=principal.org_id,
        user_id=principal.user_id,
        conversation_id=prepared.conversation_id,
        question=prepared.question,
        answer=answer,
        status=status,
        grounded=bool(cited),
        model=completion.model if completion else None,
        prompt_tokens=completion.prompt_tokens if completion else None,
        completion_tokens=completion.completion_tokens if completion else None,
        retrieval_ms=prepared.timings["retrieval"],
        generation_ms=prepared.timings.get("generation"),
    )
    session.add(query)
    session.add_all(
        QuerySource(
            query_id=query.id,
            tenant_id=principal.org_id,
            number=s.number,
            chunk_id=s.chunk_id,
            document_id=s.document_id,
            version_no=s.version_no,
            filename=s.filename,
            page_number=s.page_number,
            score=s.score,
            excerpt=s.excerpt,
            cited=s.cited,
        )
        for s in sources
    )
    await session.commit()
    log.info(
        "query_answered",
        query_id=str(query.id),
        status=status,
        grounded=query.grounded,
        sources=len(sources),
        cited=len(cited),
        prompt_tokens=query.prompt_tokens,
        completion_tokens=query.completion_tokens,
    )
    return QueryResponse(
        query_id=query.id,
        conversation_id=prepared.conversation_id,
        question=prepared.question,
        status=status,
        answer=answer,
        grounded=query.grounded,
        sources=sources,
        model=query.model,
        usage=Usage(prompt_tokens=query.prompt_tokens, completion_tokens=query.completion_tokens),
        timings_ms=prepared.timings,
        created_at=query.created_at,
    )


def _status_without_llm(prepared: Prepared, llm: ChatModel | None) -> QueryStatus | None:
    if not prepared.sources:
        return QueryStatus.NO_RELEVANT_DOCUMENTS
    if llm is None:
        return QueryStatus.LLM_UNAVAILABLE
    return None


async def answer(
    session: AsyncSession,
    settings: Settings,
    principal: Principal,
    llm: ChatModel | None,
    req: QueryRequest,
) -> QueryResponse:
    prepared = await prepare(session, settings, principal, req)
    status = _status_without_llm(prepared, llm)
    completion: Completion | None = None
    if status is None and llm is not None:
        started = time.perf_counter()
        try:
            completion = await llm.complete(prepared.messages)
            status = QueryStatus.ANSWERED
        except LLMUnavailable as exc:
            log.error("llm_unavailable", reason=exc.reason)
            status = QueryStatus.LLM_UNAVAILABLE
        prepared.timings["generation"] = round((time.perf_counter() - started) * 1000, 2)
    # (status is only None here if llm is None, which _status_without_llm already handled)
    final = status or QueryStatus.LLM_UNAVAILABLE
    return await finalize(session, principal, prepared, status=final, completion=completion)


def _sse(event: str, data: object) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


async def stream_answer(
    sessionmaker: async_sessionmaker[AsyncSession],
    principal: Principal,
    llm: ChatModel | None,
    prepared: Prepared,
) -> AsyncIterator[bytes]:
    """Server-sent events:
    - `sources`: the numbered sources, sent first.
    - `delta`: answer text as it is generated. It is raw model output, so the final answer
      in `done` may differ slightly if an invalid citation marker was removed.
    - `done`: the complete QueryResponse.
    """
    yield _sse("sources", [s.model_dump(mode="json") for s in prepared.sources])
    status = _status_without_llm(prepared, llm)
    completion: Completion | None = None
    if status is None and llm is not None:
        started = time.perf_counter()
        try:
            async for item in llm.stream(prepared.messages):
                if isinstance(item, Completion):
                    completion = item
                else:
                    yield _sse("delta", {"text": item})
            status = QueryStatus.ANSWERED
        except LLMUnavailable as exc:
            log.error("llm_unavailable", reason=exc.reason)
            status, completion = QueryStatus.LLM_UNAVAILABLE, None
        prepared.timings["generation"] = round((time.perf_counter() - started) * 1000, 2)
    final = status or QueryStatus.LLM_UNAVAILABLE
    # The request's session has closed by the time the stream runs: use a fresh one.
    async with sessionmaker() as session:
        await bind_tenant(session, principal.org_id)
        response = await finalize(session, principal, prepared, status=final, completion=completion)
    yield _sse("done", response.model_dump(mode="json"))


# --- history -----------------------------------------------------------------------------------


async def list_queries(
    session: AsyncSession,
    principal: Principal,
    *,
    conversation_id: uuid.UUID | None,
    limit: int,
    cursor: str | None,
) -> QueryHistory:
    """The caller's own questions, newest first."""
    query = select(Query).where(
        Query.tenant_id == principal.org_id, Query.user_id == principal.user_id
    )
    if conversation_id is not None:
        query = query.where(Query.conversation_id == conversation_id)
    if cursor:
        query = query.where(tuple_(Query.created_at, Query.id) < tuple_(*decode_cursor(cursor)))
    rows = list(
        (
            await session.scalars(
                query.order_by(Query.created_at.desc(), Query.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return QueryHistory(
        items=[QuerySummary.model_validate(r) for r in rows],
        next_cursor=encode_cursor(rows[-1].created_at, rows[-1].id) if has_more else None,
    )


async def get_query(
    session: AsyncSession, principal: Principal, query_id: uuid.UUID
) -> QueryResponse:
    query = await session.scalar(
        select(Query).where(
            Query.id == query_id,
            Query.tenant_id == principal.org_id,
            Query.user_id == principal.user_id,
        )
    )
    if query is None:
        raise NotFound("Query does not exist", code="QUERY_NOT_FOUND")
    sources = await session.scalars(
        select(QuerySource)
        .where(QuerySource.query_id == query.id, QuerySource.tenant_id == principal.org_id)
        .order_by(QuerySource.number)
    )
    timings = {"retrieval": query.retrieval_ms}
    if query.generation_ms is not None:
        timings["generation"] = query.generation_ms
    return QueryResponse(
        query_id=query.id,
        conversation_id=query.conversation_id,
        question=query.question,
        status=QueryStatus(query.status),
        answer=query.answer,
        grounded=query.grounded,
        sources=[SourceOut.model_validate(s) for s in sources],
        model=query.model,
        usage=Usage(prompt_tokens=query.prompt_tokens, completion_tokens=query.completion_tokens),
        timings_ms=timings,
        created_at=query.created_at,
    )
