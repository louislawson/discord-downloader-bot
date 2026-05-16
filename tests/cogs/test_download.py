"""Branch tests for the /download cog.

Patches ``download_channel_media.kiq`` at the import site so the cog's
enqueue path is exercised without a live broker. The actual task body
is tested in ``tests/tasks/test_download.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader_bot.cogs.download import Download


async def _invoke(cog, ctx, only_me=False):
    """Call the cog's command callback directly, bypassing the discord.py decorator."""
    await cog.download.callback(cog, ctx, only_me=only_me)


def _last_embed(ctx):
    return ctx.send.await_args.kwargs["embed"]


@pytest.fixture
def mock_kiq(mocker):
    """Patch ``download_channel_media.kiq`` and return the AsyncMock."""
    fake_task = MagicMock(task_id="task-abc")
    return mocker.patch(
        "downloader_bot.cogs.download.download_channel_media.kiq",
        new_callable=AsyncMock,
        return_value=fake_task,
    )


class TestPermissionPrecheck:
    async def test_missing_read_message_history_fails_fast(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        # Flip read_message_history off — cog should bail before .kiq.
        perms = MagicMock()
        perms.read_message_history = False
        mock_context.channel.permissions_for = MagicMock(return_value=perms)
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_context.defer.assert_awaited_once()
        mock_kiq.assert_not_awaited()
        assert _last_embed(mock_context).title == "Missing permission"

    async def test_passes_through_when_permission_granted(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        # Default mock_context fixture sets read_message_history=True.
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_kiq.assert_awaited_once()


class TestEnqueueHappyPath:
    async def test_enqueues_with_typed_kwargs_and_acks(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, only_me=False)

        mock_kiq.assert_awaited_once()
        # Taskiq uses typed kwargs, not a payload dict.
        assert mock_kiq.await_args.kwargs == {
            "channel_id": 555,
            "user_id": 42,
            "guild_id": 12345,
            "only_me": False,
        }
        embed = _last_embed(mock_context)
        assert embed.title == "Download queued"
        # Task id propagates into the embed footer for user-side troubleshooting.
        assert "task-abc" in embed.footer.text


class TestOnlyMe:
    async def test_only_me_propagates_and_makes_ack_ephemeral(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, only_me=True)

        # defer + send both pass ephemeral=True.
        assert mock_context.defer.await_args.kwargs == {"ephemeral": True}
        assert mock_context.send.await_args.kwargs["ephemeral"] is True
        # Task receives only_me=True.
        assert mock_kiq.await_args.kwargs["only_me"] is True


class TestDmContext:
    async def test_dm_context_passes_guild_id_none(
        self,
        mock_bot,
        dm_context,
        mock_kiq,
    ):
        # In a DM channel, context.guild is None — the cog must not
        # try to look up read_message_history on a None guild.me, and
        # must pass guild_id=None to the task.
        cog = Download(mock_bot)

        await _invoke(cog, dm_context)

        assert mock_kiq.await_args.kwargs["guild_id"] is None


class TestRateLimit:
    async def test_rate_limited_non_owner_blocks_kiq_with_ephemeral_embed(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        allow_ratelimit,
    ):
        # mock_context defaults to owner_id=999 (not the author's 42),
        # so the rate-limit branch runs. Force the bucket to deny.
        allow_ratelimit.return_value = (False, 720.0)
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        allow_ratelimit.assert_awaited_once()
        mock_kiq.assert_not_awaited()
        # Rate-limit replies are always ephemeral, regardless of only_me.
        assert mock_context.send.await_args.kwargs["ephemeral"] is True
        assert _last_embed(mock_context).title == "Rate limit reached"

    async def test_guild_owner_bypasses_rate_limit(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        allow_ratelimit,
    ):
        # Make the author the guild owner.
        mock_context.guild.owner_id = mock_context.author.id
        # Even with a "deny" verdict ready, owner-bypass means acquire
        # is never called and the enqueue proceeds.
        allow_ratelimit.return_value = (False, 720.0)
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        allow_ratelimit.assert_not_awaited()
        mock_kiq.assert_awaited_once()

    async def test_dm_context_skips_rate_limit(
        self,
        mock_bot,
        dm_context,
        mock_kiq,
        allow_ratelimit,
    ):
        # No guild → nothing to rate-limit. acquire must not be called.
        cog = Download(mock_bot)

        await _invoke(cog, dm_context)

        allow_ratelimit.assert_not_awaited()
        mock_kiq.assert_awaited_once()

    async def test_allowed_non_owner_passes_through_to_kiq(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        allow_ratelimit,
    ):
        # Default allow_ratelimit return value is (True, 0.0).
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        allow_ratelimit.assert_awaited_once()
        mock_kiq.assert_awaited_once()


class TestBrokerUnavailable:
    async def test_kiq_raises_surfaces_service_unavailable_embed(
        self,
        mock_bot,
        mock_context,
        mocker,
    ):
        # RabbitMQ went away between bot startup and command invocation.
        mocker.patch(
            "downloader_bot.cogs.download.download_channel_media.kiq",
            new_callable=AsyncMock,
            side_effect=RuntimeError("rabbitmq gone"),
        )
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_bot.logger.exception.assert_called_once()
        assert _last_embed(mock_context).title == "Service unavailable"

    async def test_broker_down_with_only_me_keeps_response_ephemeral(
        self,
        mock_bot,
        mock_context,
        mocker,
    ):
        mocker.patch(
            "downloader_bot.cogs.download.download_channel_media.kiq",
            new_callable=AsyncMock,
            side_effect=RuntimeError("rabbitmq gone"),
        )
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, only_me=True)

        # Error embed is still hidden from the channel — user-requested
        # privacy preserved even on the failure path.
        assert mock_context.send.await_args.kwargs["ephemeral"] is True
