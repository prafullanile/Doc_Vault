from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # jwt_secret is read from the environment
