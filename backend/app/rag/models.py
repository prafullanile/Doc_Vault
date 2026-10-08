import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKey


class QueryStatus(StrEnum):
    ANSWERED = "ANSWERED"
    NO_RELEVANT_DOCUMENTS = "NO_RELEVANT_DOCUMENTS"  # nothing to ground an answer on: no LLM call
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # sources are still returned


class Query(UUIDPrimaryKey, Base):
    """One question and its answer: the audit trail for every AI response (§25-27), and the
    raw data for quality evaluation and cost tracking later (§58)."""

    __tablename__ = "queries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ANSWERED', 'NO_RELEVANT_DOCUMENTS', 'LLM_UNAVAILABLE')", name="status"
        ),
        Index("ix_queries_conversation", "tenant_id", "conversation_id", "created_at"),
        Index("ix_queries_user_created", "tenant_id", "user_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    conversation_id: Mapped[uuid.UUID] = mapped_column()
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    grounded: Mapped[bool] = mapped_column(Boolean)  # the answer cites at least one source
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    retrieval_ms: Mapped[float] = mapped_column(Float)
    generation_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuerySource(Base):
    """The sources the model was given for a query, numbered as in the prompt, and whether the
    answer cited each one. Fields are copied, not referenced: re-processing a document replaces
    its chunks, but a past answer's evidence must stay inspectable."""

    __tablename__ = "query_sources"

    query_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queries.id", ondelete="CASCADE"), primary_key=True
    )
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    chunk_id: Mapped[uuid.UUID] = mapped_column()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    page_number: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    excerpt: Mapped[str] = mapped_column(Text)
    cited: Mapped[bool] = mapped_column(Boolean)
