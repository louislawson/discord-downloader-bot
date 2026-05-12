"""Cog-layer fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_bot(mock_db_pool):
    """Bot instance with the attributes Cogs read off ``self.bot``.

    No more ``arq_pool`` — Taskiq uses module-level ``download_channel_media.kiq()``
    on the imported task, not a pool attribute. The bot now carries a
    ``guild_settings_repo`` instead (used by ``/setup``).
    """
    bot = MagicMock()
    bot.db_pool = mock_db_pool
    bot.guild_settings_repo = AsyncMock()
    bot.guild_settings_repo.upsert = AsyncMock()
    bot.guild_settings_repo.get = AsyncMock()
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
    # Channel has read_message_history by default — see the perm pre-check
    # in app/cogs/download.py. Tests that want it denied flip this.
    perms = MagicMock()
    perms.read_message_history = True
    ctx.channel.permissions_for = MagicMock(return_value=perms)
    ctx.guild = MagicMock()
    ctx.guild.id = 12345
    ctx.guild.me = MagicMock()
    ctx.author = MagicMock()
    ctx.author.id = 42
    ctx.author.__str__ = lambda self: "user#0001"
    return ctx


@pytest.fixture
def dm_context(mock_context):
    """DM-context variant (``guild`` is None)."""
    mock_context.guild = None
    return mock_context
