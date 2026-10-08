"""Question answering history: queries and the sources each answer was given.

Append-only for the API role (INSERT and SELECT): an answer's record, including the evidence
it was based on, can't be edited after the fact.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]

    op.create_table(
        "queries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("grounded", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("retrieval_ms", sa.Float(), nullable=False),
        sa.Column("generation_ms", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('ANSWERED', 'NO_RELEVANT_DOCUMENTS', 'LLM_UNAVAILABLE')",
            name="ck_queries_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_queries_tenant_id_organizations"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL", name="fk_queries_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_queries"),
    )
    op.create_index(
        "ix_queries_conversation", "queries", ["tenant_id", "conversation_id", "created_at"]
    )
    op.create_index("ix_queries_user_created", "queries", ["tenant_id", "user_id", "created_at"])

    op.create_table(
        "query_sources",
        sa.Column("query_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("cited", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["queries.id"],
            ondelete="CASCADE",
            name="fk_query_sources_query_id_queries",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_query_sources_tenant_id_organizations"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_query_sources_document_id_documents"
        ),
        sa.PrimaryKeyConstraint("query_id", "number", name="pk_query_sources"),
    )

    for table in ("queries", "query_sources"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )
    op.execute(f"GRANT SELECT, INSERT ON queries, query_sources TO {app_role}")


def downgrade() -> None:
    op.drop_table("query_sources")
    op.drop_table("queries")
