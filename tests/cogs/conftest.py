"""Cog-layer fixtures."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader_bot.db.guild_settings import GuildSettings


@pytest.fixture
def mock_bot(mock_db_pool, mock_redis):
    """Bot instance with the attributes Cogs read off ``self.bot``.

    No more ``arq_pool`` — Taskiq uses module-level ``download_channel_media.kiq()``
    on the imported task, not a pool attribute. The bot now carries a
    ``guild_settings_repo`` instead (used by ``/setup`` and by the
    ``/download`` cog when computing the user→jobs index TTL) and a
    ``redis`` client (used by the per-guild rate limiter in ``/download``).
    """
    bot = MagicMock()
    bot.db_pool = mock_db_pool
    bot.guild_settings_repo = AsyncMock()
    bot.guild_settings_repo.upsert = AsyncMock()
    # Default to a guild with safe defaults (24h retention). The
    # /download cog now reads .retention_hours and multiplies by 3600,
    # which would TypeError against a bare MagicMock attribute.
    bot.guild_settings_repo.get = AsyncMock(return_value=GuildSettings(guild_id=12345))
    bot.is_owner = AsyncMock(return_value=False)
    bot.redis = mock_redis
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
    # Default to "author is NOT the owner" so the rate-limit branch is
    # exercised by default. Tests that want owner-bypass set
    # ctx.guild.owner_id = ctx.author.id explicitly.
    ctx.guild.owner_id = 999
    ctx.author = MagicMock()
    ctx.author.id = 42
    ctx.author.__str__ = lambda self: "user#0001"
    return ctx


@pytest.fixture(autouse=True)
def allow_ratelimit(mocker):
    """Patch ``ratelimit.acquire`` to allow by default for cog tests.

    Existing cog tests don't care about rate-limit state; this keeps
    them green without scattering individual patches. Tests that want
    to exercise the rate-limit branch override the return_value::

        allow_ratelimit.return_value = (False, 720.0)

    Returns the AsyncMock so individual tests can override its behavior.
    """
    return mocker.patch(
        "downloader_bot.cogs.download.ratelimit.acquire",
        new_callable=AsyncMock,
        return_value=(True, 0.0),
    )


@pytest.fixture(autouse=True)
def mock_record_job(mocker):
    """Patch ``jobs.record_job`` for cog tests.

    The real implementation uses ``redis.pipeline(transaction=True)``
    which the bare AsyncMock redis fixture doesn't model. Tests that
    care about the call inspect this mock's await_args directly.
    """
    return mocker.patch(
        "downloader_bot.cogs.download.jobs.record_job",
        new_callable=AsyncMock,
    )


@pytest.fixture
def dm_context(mock_context):
    """DM-context variant (``guild`` is None)."""
    mock_context.guild = None
    return mock_context
