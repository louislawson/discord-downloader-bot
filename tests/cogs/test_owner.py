"""Branch tests for the !sync owner command."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader_bot.cogs.owner import Owner, setup


async def _invoke(cog, ctx, scope):
    """Call the ``sync`` callback directly, bypassing ``@commands.is_owner()``."""
    await cog.sync.callback(cog, ctx, scope)


def _last_embed(ctx):
    return ctx.send.await_args.kwargs["embed"]


@pytest.fixture
def owner_context(mock_context):
    """Sync cog accesses ``context.bot.tree.{sync,copy_global_to}``.

    ``sync`` must be an ``AsyncMock`` (awaited); ``copy_global_to`` must be a
    plain ``MagicMock`` (called, not awaited — otherwise an un-awaited
    coroutine triggers the ``filterwarnings=error`` gate).
    """
    mock_context.bot = MagicMock()
    mock_context.bot.tree = MagicMock()
    mock_context.bot.tree.sync = AsyncMock()
    mock_context.bot.tree.copy_global_to = MagicMock()
    return mock_context


class TestSetup:
    async def test_setup_adds_owner_cog(self, mock_bot):
        mock_bot.add_cog = AsyncMock()

        await setup(mock_bot)

        mock_bot.add_cog.assert_awaited_once()
        assert isinstance(mock_bot.add_cog.await_args.args[0], Owner)


class TestUnknownScope:
    async def test_unknown_scope_logs_warning_and_rejects_without_syncing(
        self,
        mock_bot,
        owner_context,
    ):
        cog = Owner(mock_bot)

        await _invoke(cog, owner_context, scope="quantum")

        owner_context.bot.tree.sync.assert_not_awaited()
        owner_context.bot.tree.copy_global_to.assert_not_called()
        mock_bot.logger.warning.assert_called_once()
        embed = _last_embed(owner_context)
        assert embed.title == "Command error"
        assert "quantum" in embed.description


class TestGlobalScope:
    async def test_global_syncs_tree_without_guild_kwarg(
        self,
        mock_bot,
        owner_context,
    ):
        cog = Owner(mock_bot)

        await _invoke(cog, owner_context, scope="global")

        owner_context.bot.tree.sync.assert_awaited_once_with()
        owner_context.bot.tree.copy_global_to.assert_not_called()
        mock_bot.logger.info.assert_called_once()
        assert _last_embed(owner_context).title == "Command sync"


class TestGuildScope:
    async def test_guild_scope_in_dm_rejects_without_syncing(
        self,
        mock_bot,
        owner_context,
    ):
        owner_context.guild = None
        cog = Owner(mock_bot)

        await _invoke(cog, owner_context, scope="guild")

        owner_context.bot.tree.sync.assert_not_awaited()
        owner_context.bot.tree.copy_global_to.assert_not_called()
        embed = _last_embed(owner_context)
        assert embed.title == "Command error"
        assert "DM" in embed.description

    async def test_guild_copies_globals_then_syncs_to_current_guild(
        self,
        mock_bot,
        owner_context,
    ):
        guild = owner_context.guild
        cog = Owner(mock_bot)

        await _invoke(cog, owner_context, scope="guild")

        # copy_global_to runs *before* sync — both bound to the invoking guild.
        owner_context.bot.tree.copy_global_to.assert_called_once_with(guild=guild)
        owner_context.bot.tree.sync.assert_awaited_once_with(guild=guild)
        mock_bot.logger.info.assert_called_once()
        assert _last_embed(owner_context).title == "Command sync"
