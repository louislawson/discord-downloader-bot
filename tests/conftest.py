"""Shared pytest fixtures.

Environment variables required by ``downloader_bot.config.Settings`` are set at
module-body time before any ``downloader_bot.*`` import. The settings singleton
is constructed at import (``downloader_bot/config.py``), so per-test env
patching via ``monkeypatch.setenv`` would be too late.
"""

import os

# --- Required env (set BEFORE downloader_bot is imported anywhere) ---------
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


from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_redis():
    """AsyncMock Redis client. ``.set`` returns True (claim succeeds) by default."""
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    return redis


@pytest.fixture
def make_db_pool():
    """Factory for an asyncpg.Pool mock preloaded with a guild_settings row."""

    def _make(mode="dm", channel_id=None, row_present=True):
        pool = AsyncMock()
        if row_present:
            row = {"delivery_mode": mode, "results_channel_id": channel_id}
            pool.fetchrow = AsyncMock(return_value=row)
        else:
            pool.fetchrow = AsyncMock(return_value=None)
        pool.execute = AsyncMock(return_value="OK")
        return pool

    return _make


@pytest.fixture
def mock_db_pool(make_db_pool):
    """Default db pool mock — guild has ``mode=dm`` and no channel."""
    return make_db_pool()


@pytest.fixture
def mock_arq_pool():
    """AsyncMock ARQ pool whose ``enqueue_job`` returns a job-like mock."""
    pool = AsyncMock()
    pool.enqueue_job = AsyncMock(return_value=MagicMock(job_id="job-abc"))
    return pool


@pytest.fixture
def mock_blob_client():
    """A blob-client mock with the two attributes ``sas_url`` reads."""
    blob = MagicMock()
    blob.blob_name = "channel-media.zip"
    blob.url = "http://azurite:10000/devstoreaccount1/media/channel-media.zip"
    return blob


@pytest.fixture
def mock_azure_client(mock_blob_client):
    """Mock ContainerClient suitable for ``AzureBlobBackend(client=...)``.

    Exposes the attributes ``sas_url`` inspects: ``account_name``,
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
