import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.rag.models import QueryStatus
from app.search.schemas import SearchFilters


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Continue a conversation; omit to start a new one"
    )
    filters: SearchFilters = Field(default_factory=SearchFilters)
    stream: bool = Field(
        default=False, description="Server-sent events: sources, then answer deltas, then done"
    )


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    number: int = Field(description="The [n] used in the answer")
    document_id: uuid.UUID
    filename: str
    version_no: int
    page_number: int
    chunk_id: uuid.UUID
    score: float = Field(description="Retrieval relevance (reranker score when reranked)")
    excerpt: str
    cited: bool = Field(description="Whether the answer cites this source")


class Usage(BaseModel):
    prompt_tokens: int | None
    completion_tokens: int | None


class QueryResponse(BaseModel):
    query_id: uuid.UUID
    conversation_id: uuid.UUID
    question: str
    status: QueryStatus
    answer: str | None
    grounded: bool = Field(description="True if the answer cites at least one source")
    sources: list[SourceOut]
    model: str | None
    usage: Usage
    timings_ms: dict[str, float]
    created_at: datetime


class QuerySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    question: str
    answer: str | None
    status: QueryStatus
    grounded: bool
    created_at: datetime


class QueryHistory(BaseModel):
    items: list[QuerySummary]
    next_cursor: str | None
