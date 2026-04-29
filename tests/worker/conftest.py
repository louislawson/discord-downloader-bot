"""Worker-layer fixtures."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest


class _AsyncIter:
    """Async-iter helper for mocking ``channel.history(...)`` results.

    ``AsyncMock`` returns coroutines, but ``async for`` expects an async iterator.
    """

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


@pytest.fixture
def async_iter():
    return _AsyncIter


@pytest.fixture
def forbidden_factory():
    """Factory returning a real ``discord.Forbidden`` for ``side_effect=`` use."""

    def _make(message="Forbidden"):
        response = MagicMock(status=403, reason="Forbidden")
        return discord.Forbidden(response, message)

    return _make


@pytest.fixture
def mock_discord_client():
    """REST-only Discord client mock with ``fetch_*`` explicitly assigned.

    ``AsyncMock(spec=discord.Client)`` doesn't reliably propagate ``fetch_user`` /
    ``fetch_channel`` / ``fetch_guild`` as AsyncMock attributes across discord.py
    versions, so they're set explicitly.
    """
    client = AsyncMock(spec=discord.Client)
    client.fetch_user = AsyncMock()
    client.fetch_channel = AsyncMock()
    client.fetch_guild = AsyncMock()
    return client


@pytest.fixture
def arq_ctx(mock_discord_client, mock_db_pool, mock_redis):
    """ARQ context dict shaped to match what ``on_startup`` populates."""
    return {
        "discord_client": mock_discord_client,
        "db_pool": mock_db_pool,
        "redis": mock_redis,
        "job_id": "job-abc",
        "job_try": 1,
    }


@pytest.fixture
def make_attachment():
    """Factory: ``discord.Attachment``-shaped mock whose ``save`` writes ``payload`` into the buffer."""

    def _make(content_type="image/png", filename="x.png", payload=b"abc123"):
        att = MagicMock()
        att.content_type = content_type
        att.filename = filename

        async def _save(buffer):
            buffer.write(payload)

        att.save = AsyncMock(side_effect=_save)
        return att

    return _make


@pytest.fixture
def make_message():
    """Factory: minimal Discord message mock carrying an id and an attachments list."""

    def _make(message_id=1, attachments=()):
        msg = MagicMock()
        msg.id = message_id
        msg.attachments = list(attachments)
        return msg

    return _make


@pytest.fixture
def user_mock():
    """User mock for DM delivery tests."""
    user = AsyncMock()
    user.send = AsyncMock()
    return user


@pytest.fixture
def channel_mock():
    """Channel mock for channel-post delivery tests."""
    channel = AsyncMock()
    channel.send = AsyncMock()
    return channel
