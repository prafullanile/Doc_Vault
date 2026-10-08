from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Values come from the environment (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # The API connects as a non-owner, non-superuser role so row-level security applies.
    database_url: str = "postgresql+asyncpg://docunexus_app:app@localhost:5432/docunexus"
    database_pool_size: int = 10
    database_echo: bool = False

    jwt_secret: str = Field(min_length=32)
    jwt_issuer: str = "docunexus"
    jwt_audience: str = "docunexus-api"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60

    max_upload_bytes: int = 200 * 1024 * 1024

    # Object storage: "local" (dev/tests) or "s3" (MinIO locally, S3 in production).
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: Path = Path("./var/storage")
    s3_endpoint_url: str | None = None  # e.g. http://minio:9000; None means AWS S3
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "docunexus"

    # Background processing (see app/processing). The worker process uses DATABASE_URL too,
    # pointed at the worker role.
    worker_concurrency: int = 2
    worker_poll_interval_seconds: float = 1.0
    job_lease_seconds: int = 60
    job_max_attempts: int = 5
    retry_base_seconds: float = 10.0
    retry_max_seconds: float = 600.0
    reaper_interval_seconds: float = 15.0

    # Extraction runs in a subprocess with these limits, so a hostile file can only kill the
    # subprocess, never the worker.
    extraction_timeout_seconds: int = 300
    extraction_memory_mb: int = 2048  # enforced on POSIX only
    max_pages: int = 2000
    max_decompressed_bytes: int = 500 * 1024 * 1024
    ocr_language: str = "eng"
    ocr_dpi: int = 200
    ocr_min_chars: int = 25  # a page with less extractable text than this gets OCR'd

    # Chunking (see app/search/chunking.py). Words, not tokens: ~1.3 tokens per English word,
    # so 180 words stays well inside the embedding model's 512-token window.
    chunk_target_words: int = 180
    chunk_overlap_words: int = 30

    # Embeddings and reranking run locally on CPU via fastembed (ONNX); no paid API.
    # "hashing" is a deterministic stand-in for tests: no model download, same interface.
    embedding_backend: Literal["fastembed", "hashing"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = 32
    reranker_backend: Literal["fastembed", "none"] = "fastembed"
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    model_cache_dir: Path | None = None  # None: fastembed's default cache

    # Search: candidates fetched from each retriever, and how many of the fused list to rerank.
    search_candidates: int = 50
    rerank_candidates: int = 30

    # Question answering (RAG). Any OpenAI-compatible chat API: OpenAI, Google Gemini (via its
    # OpenAI-compatible endpoint) or a local Ollama. "fake" is a deterministic stand-in for tests.
    llm_backend: Literal["openai", "gemini", "ollama", "fake"] = "openai"
    llm_model: str | None = None  # None/empty: the backend's default model
    llm_base_url: str | None = None  # None/empty: the backend's standard URL
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 60.0
    llm_max_output_tokens: int = 800
    llm_temperature: float | None = None  # None: the model's default (some models allow no other)
    rag_context_chunks: int = 6  # sources given to the model
    rag_max_context_chars: int = 12_000
    # Sources the cross-encoder scores below this are dropped before generation, so the model
    # isn't handed irrelevant text to "answer" from. ms-marco logits: relevant passages > 0.
    rag_min_rerank_score: float = -4.0
    rag_history_turns: int = 3  # earlier Q&A pairs of the conversation sent with a follow-up


@lru_cache
def get_settings() -> Settings:
    return Settings()  # jwt_secret is read from the environment
