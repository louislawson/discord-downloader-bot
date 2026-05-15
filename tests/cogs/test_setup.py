"""Branch tests for the /setup cog — per-guild config reads & writes."""

from unittest.mock import MagicMock

from downloader_bot.cogs.setup import Setup
from downloader_bot.db.guild_settings import GuildSettings


async def _invoke(cog, ctx, **kwargs):
    """Call the ``set`` subcommand's callback directly, bypassing the decorators
    (incl. ``has_permissions``/``guild_only``, which are checked by
    discord.py's command pipeline, not the callback itself)."""
    await cog.setup_set.callback(cog, ctx, **kwargs)


async def _invoke_show(cog, ctx):
    await cog.setup_show.callback(cog, ctx)


async def _invoke_clear(cog, ctx):
    await cog.setup_clear.callback(cog, ctx)


async def _invoke_group(cog, ctx):
    await cog.setup_group.callback(cog, ctx)


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


class TestShow:
    async def test_returns_defaults_for_unconfigured_guild(
        self,
        mock_bot,
        mock_context,
    ):
        mock_bot.guild_settings_repo.get.return_value = GuildSettings(guild_id=12345)
        cog = Setup(mock_bot)

        await _invoke_show(cog, mock_context)

        embed = _last_embed(mock_context)
        assert embed.title == "Delivery settings"
        assert "`dm`" in embed.description
        assert "_not set_" in embed.description
        assert "`24h`" in embed.description
        assert mock_context.send.await_args.kwargs["ephemeral"] is True

    async def test_renders_configured_settings(self, mock_bot, mock_context):
        mock_bot.guild_settings_repo.get.return_value = GuildSettings(
            guild_id=12345,
            delivery_mode="channel",
            results_channel_id=999,
            retention_hours=48,
        )
        cog = Setup(mock_bot)

        await _invoke_show(cog, mock_context)

        embed = _last_embed(mock_context)
        assert "`channel`" in embed.description
        assert "<#999>" in embed.description
        assert "`48h`" in embed.description

    async def test_show_does_not_upsert(self, mock_bot, mock_context):
        mock_bot.guild_settings_repo.get.return_value = GuildSettings(guild_id=12345)
        cog = Setup(mock_bot)

        await _invoke_show(cog, mock_context)

        mock_bot.guild_settings_repo.upsert.assert_not_awaited()


class TestClear:
    async def test_resets_mode_and_channel(self, mock_bot, mock_context):
        mock_bot.guild_settings_repo.get.return_value = GuildSettings(
            guild_id=12345,
            delivery_mode="channel",
            results_channel_id=999,
            retention_hours=48,
        )
        cog = Setup(mock_bot)

        await _invoke_clear(cog, mock_context)

        mock_bot.guild_settings_repo.upsert.assert_awaited_once()
        new_settings = mock_bot.guild_settings_repo.upsert.await_args.args[0]
        assert new_settings.delivery_mode == "dm"
        assert new_settings.results_channel_id is None
        # Retention preserved across clear.
        assert new_settings.retention_hours == 48

    async def test_preserves_media_type_and_size_fields(
        self,
        mock_bot,
        mock_context,
    ):
        mock_bot.guild_settings_repo.get.return_value = GuildSettings(
            guild_id=12345,
            delivery_mode="channel",
            results_channel_id=999,
            allowed_media_types=["image/png", "image/jpeg"],
            max_archive_size_bytes=10_000_000,
        )
        cog = Setup(mock_bot)

        await _invoke_clear(cog, mock_context)

        new_settings = mock_bot.guild_settings_repo.upsert.await_args.args[0]
        assert new_settings.allowed_media_types == ["image/png", "image/jpeg"]
        assert new_settings.max_archive_size_bytes == 10_000_000

    async def test_success_ack_is_ephemeral(self, mock_bot, mock_context):
        mock_bot.guild_settings_repo.get.return_value = GuildSettings(guild_id=12345)
        cog = Setup(mock_bot)

        await _invoke_clear(cog, mock_context)

        assert _last_embed(mock_context).title == "Settings cleared"
        assert mock_context.send.await_args.kwargs["ephemeral"] is True


class TestGroupParent:
    async def test_emits_usage_when_no_subcommand(self, mock_bot, mock_context):
        mock_context.invoked_subcommand = None
        cog = Setup(mock_bot)

        await _invoke_group(cog, mock_context)

        embed = _last_embed(mock_context)
        assert embed.title == "Setup"
        assert "set" in embed.description
        assert "show" in embed.description
        assert "clear" in embed.description
