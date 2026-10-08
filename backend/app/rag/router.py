import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.auth.dependencies import Reader, TenantSession
from app.core.dependencies import AppSettings
from app.rag import service
from app.rag.llm import ChatModel
from app.rag.schemas import QueryHistory, QueryRequest, QueryResponse

router = APIRouter(tags=["query"])


def _llm(request: Request) -> ChatModel | None:
    llm: ChatModel | None = request.app.state.llm
    return llm


@router.post(
    "/query",
    response_model=QueryResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
    summary="Ask a question; the answer cites the document pages it is based on",
)
async def ask(
    body: QueryRequest,
    request: Request,
    principal: Reader,
    session: TenantSession,
    settings: AppSettings,
) -> Any:
    if body.stream:
        # Retrieval happens now, inside the request's session; generation then streams.
        prepared = await service.prepare(session, settings, principal, body)
        return StreamingResponse(
            service.stream_answer(
                request.app.state.sessionmaker, principal, _llm(request), prepared
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await service.answer(session, settings, principal, _llm(request), body)


@router.get("/queries")
async def list_queries(
    principal: Reader,
    session: TenantSession,
    conversation_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> QueryHistory:
    return await service.list_queries(
        session, principal, conversation_id=conversation_id, limit=limit, cursor=cursor
    )


@router.get("/queries/{query_id}")
async def get_query(
    query_id: uuid.UUID, principal: Reader, session: TenantSession
) -> QueryResponse:
    return await service.get_query(session, principal, query_id)
