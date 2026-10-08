import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class SearchMode(StrEnum):
    HYBRID = "hybrid"  # keyword + vector, fused, then reranked
    KEYWORD = "keyword"  # PostgreSQL full-text only: exact terms, names, IDs
    VECTOR = "vector"  # embeddings only: meaning, paraphrases


class SearchFilters(BaseModel):
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    mime_types: list[str] | None = Field(default=None, max_length=10)
    languages: list[str] | None = Field(default=None, max_length=10)
    created_after: datetime | None = None
    created_before: datetime | None = None


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: SearchMode = SearchMode.HYBRID
    limit: int = Field(default=10, ge=1, le=50)
    rerank: bool = True
    filters: SearchFilters = Field(default_factory=SearchFilters)


class ScoreBreakdown(BaseModel):
    """Why a result ranked where it did (§27, explainable answers)."""

    keyword_rank: int | None = Field(description="Rank in the full-text list (1 = best)")
    vector_rank: int | None = Field(description="Rank in the vector list (1 = best)")
    fused: float = Field(description="Reciprocal-rank-fusion score")
    rerank: float | None = Field(description="Cross-encoder relevance score, if reranked")


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    version_no: int
    page_number: int
    section: str | None
    text: str
    score: float = Field(description="The score results are ordered by")
    scores: ScoreBreakdown


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    reranked: bool
    results: list[SearchResult]
    warnings: list[str] = Field(
        description="Degradations, e.g. vector_search_unavailable (fell back to keyword-only)"
    )
    timings_ms: dict[str, float]
