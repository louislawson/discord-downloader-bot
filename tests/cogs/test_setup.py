"""Branch tests for the /setup cog — per-guild config writes."""

from unittest.mock import MagicMock

from downloader_bot.cogs.setup import Setup
from downloader_bot.db.guild_settings import GuildSettings


async def _invoke(cog, ctx, **kwargs):
    """Call the cog's command callback directly, bypassing the decorators
    (incl. ``has_permissions``/``guild_only``, which are checked by
    discord.py's command pipeline, not the callback itself)."""
    await cog.setup_cmd.callback(cog, ctx, **kwargs)


def _last_embed(ctx):
    return ctx.send.await_args.kwargs["embed"]


class TestValidation:
    async def test_invalid_delivery_mode_rejects_without_upsert(
        self,
        mock_bot,
        mock_context,
    ):
        cog = Setup(mock_bot)

        await _invoke(cog, mock_context, delivery_mode="quantum")

        mock_bot.guild_settings_repo.upsert.assert_not_awaited()
        assert _last_embed(mock_context).title == "Invalid delivery mode"

    async def test_channel_mode_without_results_channel_rejects(
        self,
        mock_bot,
        mock_context,
    ):
        cog = Setup(mock_bot)

        await _invoke(
            cog,
            mock_context,
            delivery_mode="channel",
            results_channel=None,
        )

        mock_bot.guild_settings_repo.upsert.assert_not_awaited()
        assert _last_embed(mock_context).title == "Missing channel"

    async def test_validation_errors_are_ephemeral(self, mock_bot, mock_context):
        cog = Setup(mock_bot)

        await _invoke(cog, mock_context, delivery_mode="quantum")

        assert mock_context.send.await_args.kwargs["ephemeral"] is True


class TestUpsert:
    async def test_dm_mode_upserts_with_correct_shape(
        self,
        mock_bot,
        mock_context,
    ):
        cog = Setup(mock_bot)

        await _invoke(cog, mock_context, delivery_mode="dm", retention_hours=12)

        mock_bot.guild_settings_repo.upsert.assert_awaited_once()
        new_settings = mock_bot.guild_settings_repo.upsert.await_args.args[0]
        assert isinstance(new_settings, GuildSettings)
        assert new_settings.guild_id == 12345
        assert new_settings.delivery_mode == "dm"
        assert new_settings.results_channel_id is None
        assert new_settings.retention_hours == 12

    async def test_channel_mode_includes_channel_id(self, mock_bot, mock_context):
        results_channel = MagicMock()
        results_channel.id = 999
        results_channel.mention = "<#999>"
        cog = Setup(mock_bot)

        await _invoke(
            cog,
            mock_context,
            delivery_mode="channel",
            results_channel=results_channel,
        )

        new_settings = mock_bot.guild_settings_repo.upsert.await_args.args[0]
        assert new_settings.delivery_mode == "channel"
        assert new_settings.results_channel_id == 999

    async def test_retention_hours_default_24(self, mock_bot, mock_context):
        cog = Setup(mock_bot)

        await _invoke(cog, mock_context, delivery_mode="dm")

        new_settings = mock_bot.guild_settings_repo.upsert.await_args.args[0]
        assert new_settings.retention_hours == 24

    async def test_success_ack_is_ephemeral(self, mock_bot, mock_context):
        cog = Setup(mock_bot)

        await _invoke(cog, mock_context, delivery_mode="dm")

        assert _last_embed(mock_context).title == "Settings updated"
        assert mock_context.send.await_args.kwargs["ephemeral"] is True

    async def test_success_embed_mentions_channel_when_set(
        self,
        mock_bot,
        mock_context,
    ):
        results_channel = MagicMock()
        results_channel.id = 999
        results_channel.mention = "<#999>"
        cog = Setup(mock_bot)

        await _invoke(
            cog,
            mock_context,
            delivery_mode="channel",
            results_channel=results_channel,
        )

        embed = _last_embed(mock_context)
        assert "<#999>" in embed.description
