"""Branch tests for the /download cog.

Patches ``download_channel_media.kiq`` at the import site so the cog's
enqueue path is exercised without a live broker. The actual task body
is tested in ``tests/tasks/test_download.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader_bot.cogs.download import Download


async def _invoke(cog, ctx, **kwargs):
    """Call the cog's command callback directly, bypassing the discord.py decorator."""
    await cog.download.callback(cog, ctx, **kwargs)


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

        await _invoke(cog, mock_context, dm_me=False)

        mock_kiq.assert_awaited_once()
        # Taskiq uses typed kwargs, not a payload dict.
        assert mock_kiq.await_args.kwargs == {
            "channel_id": 555,
            "user_id": 42,
            "guild_id": 12345,
            "dm_me": False,
            "filters": None,
        }
        embed = _last_embed(mock_context)
        assert embed.title == "Download queued"
        # Task id propagates into the embed footer for user-side troubleshooting.
        assert "task-abc" in embed.footer.text


class TestDmMe:
    async def test_dm_me_propagates_and_makes_ack_ephemeral(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, dm_me=True)

        # defer + send both pass ephemeral=True.
        assert mock_context.defer.await_args.kwargs == {"ephemeral": True}
        assert mock_context.send.await_args.kwargs["ephemeral"] is True
        # Task receives dm_me=True.
        assert mock_kiq.await_args.kwargs["dm_me"] is True


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
        # Rate-limit replies are always ephemeral, regardless of dm_me.
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


class TestRecordJob:
    async def test_records_job_with_consumed_token_for_regular_user(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        mock_record_job,
    ):
        # Regular user (author 42, owner 999) → rate-limit acquire runs,
        # so enqueue_consumed_token must be True in the recorded meta.
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_record_job.assert_awaited_once()
        meta = mock_record_job.await_args.kwargs["meta"]
        assert meta["owner_user_id"] == 42
        assert meta["guild_id"] == 12345
        assert meta["channel_id"] == 555
        assert meta["enqueue_consumed_token"] is True

    async def test_guild_owner_bypass_records_consumed_token_false(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        mock_record_job,
    ):
        # Guild owner = author → rate-limit skipped → no token consumed.
        # The cancel path uses this flag to skip the refund.
        mock_context.guild.owner_id = mock_context.author.id
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        meta = mock_record_job.await_args.kwargs["meta"]
        assert meta["enqueue_consumed_token"] is False

    async def test_dm_context_records_guild_id_none_and_no_token(
        self,
        mock_bot,
        dm_context,
        mock_kiq,
        mock_record_job,
    ):
        # DM context → no guild bucket to consume → guild_id None,
        # enqueue_consumed_token False (cancel skips refund cleanly).
        cog = Download(mock_bot)

        await _invoke(cog, dm_context)

        meta = mock_record_job.await_args.kwargs["meta"]
        assert meta["guild_id"] is None
        assert meta["enqueue_consumed_token"] is False

    async def test_ttl_seconds_matches_guild_retention(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        mock_record_job,
    ):
        # Bump retention to 72h; cog must pass ttl_seconds = 72 * 3600.
        from downloader_bot.db.guild_settings import GuildSettings

        mock_bot.guild_settings_repo.get = AsyncMock(
            return_value=GuildSettings(guild_id=12345, retention_hours=72)
        )
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        ttl = mock_record_job.await_args.kwargs["ttl_seconds"]
        assert ttl == 72 * 3600

    async def test_record_job_failure_does_not_abort_ack(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
        mock_record_job,
    ):
        # Redis blip during record_job is best-effort — the worker
        # still delivers, so the user must still get the ack embed.
        mock_record_job.side_effect = RuntimeError("redis down")
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_kiq.assert_awaited_once()
        mock_context.send.assert_awaited_once()
        assert _last_embed(mock_context).title == "Download queued"
        mock_bot.logger.warning.assert_called_once()

    async def test_broker_down_with_dm_me_keeps_response_ephemeral(
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

        await _invoke(cog, mock_context, dm_me=True)

        # Error embed is still hidden from the channel — user-requested
        # privacy preserved even on the failure path.
        assert mock_context.send.await_args.kwargs["ephemeral"] is True


class TestFilterValidation:
    async def test_during_with_before_rejected_before_kiq(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, during="last-week", before="7d")

        mock_kiq.assert_not_awaited()
        embed = _last_embed(mock_context)
        assert embed.title == "Conflicting filters"

    async def test_during_with_after_rejected_before_kiq(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, during="this-month", after="30d")

        mock_kiq.assert_not_awaited()
        assert _last_embed(mock_context).title == "Conflicting filters"

    async def test_invalid_before_duration_rejected_before_kiq(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, before="7q")

        mock_kiq.assert_not_awaited()
        embed = _last_embed(mock_context)
        assert embed.title == "Invalid filter"
        # The validator's message should call out the unit it expected.
        assert "7q" in embed.description

    async def test_invalid_after_duration_rejected_before_kiq(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, after="abc")

        mock_kiq.assert_not_awaited()
        assert _last_embed(mock_context).title == "Invalid filter"


class TestFilterPayload:
    async def test_filters_dict_is_built_and_passed_to_kiq(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)
        from_user = MagicMock()
        from_user.id = 777

        await _invoke(
            cog,
            mock_context,
            media_type="image",
            from_user=from_user,
            after="7d",
        )

        filters_payload = mock_kiq.await_args.kwargs["filters"]
        assert filters_payload == {
            "category": "image",
            "from_user_id": 777,
            "after": "7d",
        }

    async def test_unset_filters_send_none(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        # No filter args → filters=None reaches the task. The task's
        # `filters or {}` then takes its no-filters fast path.
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        assert mock_kiq.await_args.kwargs["filters"] is None

    async def test_during_alone_sends_only_during(
        self,
        mock_bot,
        mock_context,
        mock_kiq,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, during="today")

        assert mock_kiq.await_args.kwargs["filters"] == {"during": "today"}
