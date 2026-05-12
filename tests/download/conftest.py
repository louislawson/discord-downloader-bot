"""Download-layer fixtures.

Most fixtures live in the top-level ``tests/conftest.py`` so they're
visible to ``tests/tasks/`` too. The ones here are specific to the
streaming-zip pipeline (``async_iter``, ``make_attachment``,
``make_message``) — Discord-history-shape helpers that don't earn
their keep outside this subtree.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

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
def make_attachment():
    """Factory for ``discord.Attachment``-shaped mocks."""

    def _make(
        *,
        url: str = "https://cdn.example/x.png",
        filename: str = "x.png",
        content_type: str = "image/png",
    ):
        att = MagicMock()
        att.url = url
        att.filename = filename
        att.content_type = content_type
        return att

    return _make


@pytest.fixture
def make_message():
    """Factory: minimal Discord message mock carrying an id, created_at, attachments."""

    def _make(message_id=1, attachments=(), created_at=None):
        msg = MagicMock()
        msg.id = message_id
        msg.created_at = created_at or datetime(2026, 1, 1, tzinfo=UTC)
        msg.attachments = list(attachments)
        return msg

    return _make
