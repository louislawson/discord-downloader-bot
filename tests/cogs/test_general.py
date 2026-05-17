"""Branch tests for the /invite cog."""

from unittest.mock import AsyncMock

import pytest

from downloader_bot.cogs.general import General, setup

_INVITE_LINK = "https://discord.com/invite?botid=123"


async def _invoke(cog, ctx):
    """Call the cog's command callback directly, bypassing the discord.py decorator."""
    await cog.invite.callback(cog, ctx)


@pytest.fixture(autouse=True)
def _set_invite_link(mock_bot):
    """The /invite command reads ``bot.invite_link`` off the bot."""
    mock_bot.invite_link = _INVITE_LINK


@pytest.fixture(autouse=True)
def _author_send_async(mock_context):
    """``mock_context.author`` is a plain ``MagicMock`` in the shared fixture,
    so ``author.send`` isn't awaitable by default. The /invite cog awaits it."""
    mock_context.author.send = AsyncMock()


class TestDmSucceeds:
    async def test_dm_send_then_channel_ack_string(self, mock_bot, mock_context):
        cog = General(mock_bot)

        await _invoke(cog, mock_context)

        # DM carries the invite embed with the link in the description.
        mock_context.author.send.assert_awaited_once()
        dm_embed = mock_context.author.send.await_args.kwargs["embed"]
        assert _INVITE_LINK in dm_embed.description
        assert dm_embed.title == "Bot Invite"
        # Channel ack is a plain string with no embed / no ephemeral flag.
        mock_context.send.assert_awaited_once_with("I sent you a private message!")


class TestSetup:
    async def test_setup_adds_general_cog(self, mock_bot):
        mock_bot.add_cog = AsyncMock()

        await setup(mock_bot)

        mock_bot.add_cog.assert_awaited_once()
        assert isinstance(mock_bot.add_cog.await_args.args[0], General)


class TestDmBlocked:
    async def test_forbidden_falls_back_to_ephemeral_channel_embed(
        self,
        mock_bot,
        mock_context,
        forbidden_factory,
    ):
        mock_context.author.send.side_effect = forbidden_factory()
        cog = General(mock_bot)

        await _invoke(cog, mock_context)

        # Only the fallback channel send happens — the post-DM ack is skipped.
        mock_context.send.assert_awaited_once()
        kwargs = mock_context.send.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert _INVITE_LINK in kwargs["embed"].description
