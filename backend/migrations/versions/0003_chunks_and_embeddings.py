"""Chunks, embeddings and search indexes (Phase 4).

- ``document_chunks``: page-bounded chunks, with a generated ``tsvector`` column and a GIN index
  for full-text search.
- ``chunk_embeddings``: one 384-d vector per chunk per model, with an HNSW index for cosine
  similarity (pgvector).
- Two new pipeline stages (CHUNK, EMBED). Versions processed before this migration are queued
  for chunking, so existing documents become searchable without re-uploading.

The ``vector`` extension needs a superuser (or a pre-installed extension) to create.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
OLD_JOB_TYPES = "job_type IN ('EXTRACT_TEXT', 'DETECT_LANGUAGE')"
NEW_JOB_TYPES = "job_type IN ('EXTRACT_TEXT', 'DETECT_LANGUAGE', 'CHUNK', 'EMBED')"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    worker_role = context.config.attributes["worker_role"]

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("document_versions", sa.Column("chunk_count", sa.Integer(), nullable=True))

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_document_chunks_tenant_id_organizations"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
            name="fk_document_chunks_version_id_document_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint("version_id", "position", name="uq_document_chunks_version_position"),
    )
    op.create_index(
        "ix_document_chunks_search_vector",
        "document_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            ondelete="CASCADE",
            name="fk_chunk_embeddings_chunk_id_document_chunks",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_chunk_embeddings_tenant_id_organizations"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
            name="fk_chunk_embeddings_version_id_document_versions",
        ),
        sa.PrimaryKeyConstraint("chunk_id", "model", name="pk_chunk_embeddings"),
    )
    op.create_index(
        "ix_chunk_embeddings_hnsw",
        "chunk_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_chunk_embeddings_version_model", "chunk_embeddings", ["version_id", "model"]
    )

    # New pipeline stages.
    op.drop_constraint("ck_processing_jobs_job_type", "processing_jobs", type_="check")
    op.create_check_constraint("ck_processing_jobs_job_type", "processing_jobs", NEW_JOB_TYPES)

    for table in ("document_chunks", "chunk_embeddings"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )
    op.execute(f"GRANT SELECT ON document_chunks, chunk_embeddings TO {app_role}")
    op.execute(
        f"GRANT SELECT, INSERT, DELETE ON document_chunks, chunk_embeddings TO {worker_role}"
    )

    # Make already-processed documents searchable.
    op.execute(
        """
        INSERT INTO processing_jobs (id, tenant_id, document_id, document_version_id, job_type,
            status, max_attempts)
        SELECT gen_random_uuid(), tenant_id, document_id, id, 'CHUNK', 'QUEUED', 5
        FROM document_versions
        WHERE deleted_at IS NULL AND status = 'PROCESSED'
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM processing_jobs WHERE job_type IN ('CHUNK', 'EMBED')")
    op.drop_constraint("ck_processing_jobs_job_type", "processing_jobs", type_="check")
    op.create_check_constraint("ck_processing_jobs_job_type", "processing_jobs", OLD_JOB_TYPES)
    op.drop_table("chunk_embeddings")
    op.drop_table("document_chunks")
    op.drop_column("document_versions", "chunk_count")
    # The vector extension is left installed: other databases objects may use it.
