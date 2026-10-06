"""Document versions, extracted pages and the processing job queue.

- File-level columns move from ``documents`` to ``document_versions``. Existing documents
  become version 1, so no data is lost.
- ``processing_jobs`` / ``job_attempts`` form the PostgreSQL job queue (ADR-0004).
- A third role, the *worker*, may see every tenant's queue rows (it has to, to claim work), but
  is still tenant-scoped by RLS on documents, versions and pages: it binds the job's tenant
  before touching document data. The API role only ever sees its own tenant's jobs.

Note: the data copy runs as the migration owner. It must be a superuser or have BYPASSRLS,
otherwise FORCE ROW LEVEL SECURITY would hide the rows being copied.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
STATUS_CHECK = "status IN ('QUEUED', 'PROCESSING', 'PROCESSED', 'FAILED')"
JOB_STATUSES = "'QUEUED', 'RUNNING', 'RETRYING', 'COMPLETED', 'FAILED', 'CANCELLED', 'DEAD_LETTER'"


def _now() -> sa.TextClause:
    return sa.text("now()")


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    worker_role = context.config.attributes["worker_role"]

    # --- document_versions ----------------------------------------------------------------
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), server_default="QUEUED", nullable=False),
        sa.Column("pipeline_version", sa.String(32), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("ocr_page_count", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(STATUS_CHECK, name="ck_document_versions_status"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_document_versions_size_non_negative"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_document_versions_tenant_id_organizations"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_document_versions_document_id_documents"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_document_versions_created_by_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint("document_id", "version_no", name="uq_document_versions_document_no"),
    )

    # Existing documents become version 1.
    op.execute(
        """
        INSERT INTO document_versions (id, tenant_id, document_id, version_no, filename,
            mime_type, size_bytes, checksum, storage_key, status, created_by, created_at,
            deleted_at)
        SELECT gen_random_uuid(), tenant_id, id, 1, filename, mime_type, size_bytes, checksum,
            storage_key, status, created_by, created_at, deleted_at
        FROM documents
        """
    )
    op.add_column("documents", sa.Column("current_version_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE documents d SET current_version_id = v.id "
        "FROM document_versions v WHERE v.document_id = d.id"
    )
    op.create_foreign_key(
        "fk_documents_current_version_id_document_versions",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
    )

    op.drop_index("uq_documents_tenant_checksum_active", table_name="documents")
    op.drop_constraint("ck_documents_size_non_negative", "documents", type_="check")
    for column in ("mime_type", "size_bytes", "checksum", "storage_key"):
        op.drop_column("documents", column)

    op.create_index(
        "uq_document_versions_tenant_checksum_active",
        "document_versions",
        ["tenant_id", "checksum"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- document_pages -------------------------------------------------------------------
    op.create_table(
        "document_pages",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("source IN ('TEXT', 'OCR', 'EMPTY')", name="ck_document_pages_source"),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
            name="fk_document_pages_version_id_document_versions",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_document_pages_tenant_id_organizations"
        ),
        sa.PrimaryKeyConstraint("version_id", "page_number", name="pk_document_pages"),
    )

    # --- processing_jobs ------------------------------------------------------------------
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), server_default="QUEUED", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False
        ),
        sa.Column("locked_by", sa.String(128), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(f"status IN ({JOB_STATUSES})", name="ck_processing_jobs_status"),
        sa.CheckConstraint(
            "job_type IN ('EXTRACT_TEXT', 'DETECT_LANGUAGE')", name="ck_processing_jobs_job_type"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_processing_jobs_tenant_id_organizations"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_processing_jobs_document_id_documents"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name="fk_processing_jobs_document_version_id_document_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_jobs"),
    )
    op.create_index(
        "ix_processing_jobs_claimable",
        "processing_jobs",
        ["priority", "next_attempt_at"],
        postgresql_where=sa.text("status IN ('QUEUED', 'RETRYING')"),
    )
    op.create_index(
        "ix_processing_jobs_running_lease",
        "processing_jobs",
        ["locked_until"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_index(
        "uq_processing_jobs_active_stage",
        "processing_jobs",
        ["document_version_id", "job_type"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'RETRYING')"),
    )
    op.create_index(
        "ix_processing_jobs_document",
        "processing_jobs",
        ["tenant_id", "document_id", "created_at"],
    )

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('SUCCEEDED', 'RETRYABLE_ERROR', 'PERMANENT_ERROR', "
            "'LEASE_EXPIRED', 'CANCELLED')",
            name="ck_job_attempts_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            ondelete="CASCADE",
            name="fk_job_attempts_job_id_processing_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["organizations.id"], name="fk_job_attempts_tenant_id_organizations"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_attempts"),
    )
    op.create_index("ix_job_attempts_job", "job_attempts", ["job_id", "attempt"])

    # Documents uploaded before this migration were never processed: queue them now.
    op.execute(
        """
        INSERT INTO processing_jobs (id, tenant_id, document_id, document_version_id, job_type,
            status, max_attempts)
        SELECT gen_random_uuid(), tenant_id, document_id, id, 'EXTRACT_TEXT', 'QUEUED', 5
        FROM document_versions
        WHERE deleted_at IS NULL AND status = 'QUEUED'
        """
    )

    # --- row-level security ---------------------------------------------------------------
    for table in ("document_versions", "document_pages", "processing_jobs", "job_attempts"):
        _tenant_policy(table)
    # Policies are permissive (OR-ed): the worker additionally sees every tenant's queue rows.
    for table in ("processing_jobs", "job_attempts"):
        op.execute(
            f"CREATE POLICY worker_queue_access ON {table} TO {worker_role} "
            "USING (true) WITH CHECK (true)"
        )

    # --- privileges -----------------------------------------------------------------------
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON document_versions TO {app_role}")
    op.execute(f"GRANT SELECT ON document_pages, job_attempts TO {app_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON processing_jobs TO {app_role}")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {worker_role}")
    op.execute(f"GRANT SELECT, UPDATE ON documents, document_versions TO {worker_role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON document_pages TO {worker_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON processing_jobs, job_attempts TO {worker_role}")


def downgrade() -> None:
    worker_role = context.config.attributes["worker_role"]
    op.drop_table("job_attempts")
    op.drop_table("processing_jobs")
    op.drop_table("document_pages")

    for column, type_ in (
        ("mime_type", sa.String(127)),
        ("size_bytes", sa.BigInteger()),
        ("checksum", sa.String(64)),
        ("storage_key", sa.String(512)),
    ):
        op.add_column("documents", sa.Column(column, type_, nullable=True))
    # Restore each document from its current version (older versions are lost).
    op.execute(
        "UPDATE documents d SET mime_type = v.mime_type, size_bytes = v.size_bytes, "
        "checksum = v.checksum, storage_key = v.storage_key "
        "FROM document_versions v WHERE v.id = d.current_version_id"
    )
    for column in ("mime_type", "size_bytes", "checksum", "storage_key"):
        op.alter_column("documents", column, nullable=False)
    op.create_check_constraint("ck_documents_size_non_negative", "documents", "size_bytes >= 0")
    op.create_index(
        "uq_documents_tenant_checksum_active",
        "documents",
        ["tenant_id", "checksum"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_constraint(
        "fk_documents_current_version_id_document_versions", "documents", type_="foreignkey"
    )
    op.drop_column("documents", "current_version_id")
    op.drop_table("document_versions")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {worker_role}")
