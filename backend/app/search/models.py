import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, UUIDPrimaryKey
from app.search.embeddings import EMBEDDING_DIM

# Full-text configuration baked into the generated column. Changing it rewrites the column, so
# it lives in a migration, not in settings.
FTS_CONFIG = "english"


class DocumentChunk(UUIDPrimaryKey, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "position", name="uq_document_chunks_version_position"),
        Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    # Maintained by PostgreSQL itself, so it can never drift from `text`.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed(f"to_tsvector('{FTS_CONFIG}', text)", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChunkEmbedding(Base):
    """One vector per chunk per model. Keying by model means a new model can be backfilled
    next to the old one and switched over without downtime (same dimension only)."""

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        # Approximate nearest-neighbour index for cosine distance (the `<=>` operator).
        Index(
            "ix_chunk_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunk_embeddings_version_model", "version_id", "model"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
