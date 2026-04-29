"""Cog-layer fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_bot(mock_arq_pool, mock_db_pool):
    """Bot instance with the attributes Cogs read off ``self.bot``."""
    bot = MagicMock()
    bot.arq_pool = mock_arq_pool
    bot.db_pool = mock_db_pool
    bot.logger = MagicMock()
    bot.bot_prefix = "!"
    return bot


@pytest.fixture
def mock_context():
    """Guild-context discord.py command context."""
    ctx = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.channel = MagicMock()
    ctx.channel.id = 555
    ctx.guild = MagicMock()
    ctx.guild.id = 12345
    ctx.author = MagicMock()
    ctx.author.id = 42
    ctx.author.__str__ = lambda self: "user#0001"
    return ctx


@pytest.fixture
def dm_context(mock_context):
    """DM-context variant (``guild`` is None)."""
    mock_context.guild = None
    return mock_context
