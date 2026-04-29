"""Regression tests for ``DiscordBot.on_command_error``.

Imports are now possible because ``downloader_bot/bot.py`` gates ``bot.run(...)``
behind ``if __name__ == "__main__":``. Constructing ``DiscordBot()`` does not
open a gateway connection.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from downloader_bot.bot import DiscordBot


@pytest.fixture
def bot():
    """Real DiscordBot — discord.py's Bot.__init__ does not connect."""
    instance = DiscordBot()
    instance.logger = MagicMock()
    return instance


@pytest.fixture
def mock_context():
    ctx = AsyncMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = 42
    ctx.channel = MagicMock()
    ctx.command = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 12345
    ctx.guild.name = "Test Guild"
    return ctx


class TestCommandNotFound:
    """The regression guard — ``CommandNotFound`` must stay silent."""

    async def test_silently_ignores_unknown_command(self, bot, mock_context):
        await bot.on_command_error(mock_context, commands.CommandNotFound("nope"))

        mock_context.send.assert_not_awaited()
        bot.logger.exception.assert_not_called()


class TestKnownErrors:
    async def test_missing_permissions_sends_embed_listing_perms(
        self,
        bot,
        mock_context,
    ):
        error = commands.MissingPermissions(missing_permissions=["manage_messages"])

        await bot.on_command_error(mock_context, error)

        mock_context.send.assert_awaited_once()
        embed = mock_context.send.await_args.kwargs["embed"]
        assert "manage_messages" in embed.description

    async def test_command_on_cooldown_sends_slow_down_embed(
        self,
        bot,
        mock_context,
    ):
        cooldown = commands.Cooldown(rate=1, per=10)
        error = commands.CommandOnCooldown(cooldown, retry_after=5.0, type=None)

        await bot.on_command_error(mock_context, error)

        mock_context.send.assert_awaited_once()
        embed = mock_context.send.await_args.kwargs["embed"]
        assert "slow down" in embed.description.lower()


class TestUnhandledError:
    async def test_falls_through_to_logged_unexpected_embed(self, bot, mock_context):
        # ``CommandError`` itself is not caught by any specific branch and falls
        # through to the catch-all "Unexpected error" handler.
        error = commands.CommandError("something weird")

        await bot.on_command_error(mock_context, error)

        bot.logger.exception.assert_called_once()
        mock_context.send.assert_awaited_once()
        embed = mock_context.send.await_args.kwargs["embed"]
        assert embed.title == "Unexpected error"
