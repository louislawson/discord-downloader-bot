"""Shared pytest fixtures.

Environment variables required by ``downloader_bot.config.Settings`` are set at
module-body time before any ``downloader_bot.*`` import. The settings singleton is
constructed at import (``downloader_bot/config.py``), so per-test env patching via
``monkeypatch.setenv`` would be too late.
"""

import os

# --- Required env (set BEFORE downloader_bot is imported anywhere) --------------------
os.environ.setdefault("TOKEN", "test-token")
os.environ.setdefault("PREFIX", "!")
os.environ.setdefault(
    "AZURE_CONN_STR",
    "DefaultEndpointsProtocol=https;AccountName=testaccount;"
    "AccountKey=dGVzdGtleQ==;EndpointSuffix=core.windows.net",
)
os.environ.setdefault("AZURE_CONTAINER", "media")
os.environ.setdefault("POSTGRES_DSN", "postgresql://test:test@localhost/test")
os.environ.setdefault(
    "ALLOWED_MEDIA_TYPES",
    '["image/png", "image/jpeg", "video/mp4"]',
)
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("AZURE_INT_URL", "http://azurite:10000")
os.environ.setdefault("AZURE_EXT_URL", "http://localhost:10000")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest

from downloader_bot.db.guild_settings import GuildSettings
from downloader_bot.storage.base import StorageBackend


@pytest.fixture
def mock_redis():
    """AsyncMock Redis client.

    Defaults to "no prior idempotency state":
    - ``.get`` returns ``None`` (no cached archive_url)
    - ``.exists`` returns ``0`` (delivered marker not set)
    - ``.set`` returns ``True``
    """
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.exists = AsyncMock(return_value=0)
    redis.set = AsyncMock(return_value=True)
    redis.aclose = AsyncMock()
    return redis


@pytest.fixture
def make_db_pool():
    """Factory for an asyncpg.Pool mock that supports ``async with pool.acquire()``.

    ``GuildSettingsRepo`` accesses the pool via::

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(...)

    so the mock returns an async-context-manager-shaped object whose
    ``__aenter__`` yields a connection mock with the methods the repo calls.
    """

    def _make(
        *,
        mode: str = "dm",
        channel_id: int | None = None,
        allowed_media_types: list[str] | None = None,
        max_archive_size_bytes: int | None = None,
        retention_hours: int = 24,
        row_present: bool = True,
    ):
        if row_present:
            row = {
                "guild_id": 12345,
                "delivery_mode": mode,
                "results_channel_id": channel_id,
                "allowed_media_types": allowed_media_types,
                "max_archive_size_bytes": max_archive_size_bytes,
                "retention_hours": retention_hours,
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        else:
            row = None

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=row)
        conn.execute = AsyncMock(return_value="OK")

        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)
        pool.close = AsyncMock()
        # Expose the connection mock so tests can inspect calls.
        pool._conn = conn
        return pool

    return _make


@pytest.fixture
def mock_db_pool(make_db_pool):
    """Default db-pool mock — guild has ``mode=dm`` and no channel."""
    return make_db_pool()


@pytest.fixture
def mock_blob_client():
    """A blob-client mock with the two attributes ``upload_and_sign`` reads."""
    blob = MagicMock()
    blob.blob_name = "channel-media.zip"
    blob.url = "http://azurite:10000/devstoreaccount1/media/channel-media.zip"
    return blob


@pytest.fixture
def mock_azure_client(mock_blob_client):
    """Mock ContainerClient suitable for ``AzureBlobBackend(client=...)``.

    Exposes the attributes ``upload_and_sign`` inspects: ``account_name``,
    ``container_name``, and ``credential.account_key`` (truthy).
    """
    client = AsyncMock()
    client.account_name = "testaccount"
    client.container_name = "media"
    credential = MagicMock()
    credential.account_key = "dGVzdGtleQ=="
    client.credential = credential
    client.upload_blob = AsyncMock(return_value=mock_blob_client)
    client.close = AsyncMock()
    return client


# --- Cross-directory fixtures (used by tests/tasks/ and tests/download/) ---


@pytest.fixture
def forbidden_factory():
    """Factory returning a real ``discord.Forbidden`` for ``side_effect=`` use."""

    def _make(message="Forbidden"):
        response = MagicMock(status=403, reason="Forbidden")
        return discord.Forbidden(response, message)

    return _make


@pytest.fixture
def not_found_factory():
    """Factory returning a real ``discord.NotFound`` for ``side_effect=`` use."""

    def _make(message="Not Found"):
        response = MagicMock(status=404, reason="Not Found")
        return discord.NotFound(response, message)

    return _make


@pytest.fixture
def mock_discord_client():
    """REST-only Discord client mock with ``fetch_*`` explicitly assigned.

    ``AsyncMock(spec=discord.Client)`` doesn't reliably propagate ``fetch_user``/
    ``fetch_channel``/``fetch_guild`` as AsyncMock attributes across discord.py
    versions, so they're set explicitly.
    """
    client = AsyncMock(spec=discord.Client)
    client.fetch_user = AsyncMock()
    client.fetch_channel = AsyncMock()
    client.fetch_guild = AsyncMock()
    return client


@pytest.fixture
def mock_aiohttp_session():
    """Mock ``aiohttp.ClientSession`` for the streaming download pipeline."""
    return AsyncMock(spec=aiohttp.ClientSession)


@pytest.fixture
def mock_storage_backend():
    """Mock ``StorageBackend`` with the methods the orchestrator calls."""
    backend = AsyncMock(spec=StorageBackend)
    backend.upload_and_sign = AsyncMock(return_value="https://example/signed?sas")
    backend.delete_blob = AsyncMock()
    return backend


@pytest.fixture
def make_settings_repo():
    """Factory: ``GuildSettingsRepo`` mock returning the given settings on ``.get``."""

    def _make(guild_settings: GuildSettings | None = None):
        repo = MagicMock()
        repo.get = AsyncMock(
            return_value=guild_settings
            if guild_settings is not None
            else GuildSettings(guild_id=12345)
        )
        repo.upsert = AsyncMock()
        return repo

    return _make


@pytest.fixture
def mock_settings_repo(make_settings_repo):
    """Default repo mock — returns DM-mode defaults for any guild."""
    return make_settings_repo()


@pytest.fixture
def user_mock():
    """User mock for DM delivery tests."""
    user = AsyncMock()
    user.send = AsyncMock()
    return user


@pytest.fixture
def channel_mock():
    """Channel mock (Messageable) for channel-post delivery tests."""
    channel = MagicMock(spec=discord.abc.Messageable)
    channel.send = AsyncMock()
    return channel


@pytest.fixture
def task_context():
    """Mock Taskiq ``Context`` factory with a configurable task_id."""

    def _make(task_id: str = "task-abc"):
        ctx = MagicMock()
        ctx.message = MagicMock()
        ctx.message.task_id = task_id
        ctx.state = MagicMock()
        return ctx

    return _make
