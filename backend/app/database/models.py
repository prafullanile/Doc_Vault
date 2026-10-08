"""Imports every model so ``Base.metadata`` is complete (used by Alembic and tests)."""

from app.audit.models import AuditLog
from app.auth.models import RefreshToken, User
from app.database.base import Base
from app.documents.models import Document, DocumentPage, DocumentVersion
from app.organizations.models import Membership, Organization
from app.processing.models import JobAttempt, ProcessingJob
from app.rag.models import Query, QuerySource
from app.search.models import ChunkEmbedding, DocumentChunk

__all__ = [
    "AuditLog",
    "Base",
    "ChunkEmbedding",
    "Document",
    "DocumentChunk",
    "DocumentPage",
    "DocumentVersion",
    "JobAttempt",
    "Membership",
    "Organization",
    "ProcessingJob",
    "Query",
    "QuerySource",
    "RefreshToken",
    "User",
]
