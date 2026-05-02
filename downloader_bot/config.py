"""Centralised application settings."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Discord
    TOKEN: str
    PREFIX: str
    INVITE_LINK: str | None = None

    # Runtime
    LOGGING_LEVEL: str = "INFO"
    ENVIRONMENT: Literal["dev", "prod"] = "prod"

    # Media filter — pydantic auto-parses JSON for list-typed fields
    ALLOWED_MEDIA_TYPES: list[str] = Field(default_factory=list)

    STORAGE_BACKEND: Literal["azure"] = "azure"

    # Azure Blob Storage
    AZURE_CONN_STR: str
    AZURE_CONTAINER: str = "media"
    AZURE_INT_URL: str | None = None
    AZURE_EXT_URL: str | None = None

    # Redis / ARQ work queue
    REDIS_URL: str = "redis://redis:6379/0"

    # Postgres (per-guild settings)
    POSTGRES_DSN: str

    # Streaming-zip pipeline — chunk size for the per-attachment HTTP read
    # passed to aiohttp's iter_chunked. Doubles as the upper bound on per-job
    # in-flight bytes from the CDN.
    ATTACHMENT_CHUNK_SIZE: int = 64 * 1024


settings = Settings()
