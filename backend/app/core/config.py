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

    storage_local_dir: Path = Path("./var/storage")
    max_upload_bytes: int = 200 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()  # jwt_secret is read from the environment
