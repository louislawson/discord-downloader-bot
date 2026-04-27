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

    # Azure Blob Storage
    ST_CONN_STR: str
    ST_CONTAINER: str = "media"
    ST_INT_URL: str | None = None
    ST_EXT_URL: str | None = None

    # Redis / ARQ work queue
    REDIS_URL: str = "redis://redis:6379/0"


settings = Settings()
