"""Branch tests for the /download cog — enqueue/ack happy path + error embeds."""

from unittest.mock import AsyncMock

from downloader_bot.cogs.download import Download


async def _invoke(cog, ctx, only_me=False):
    """Call the cog's command callback directly, bypassing the discord.py decorator."""
    await cog.download.callback(cog, ctx, only_me=only_me)


def _last_embed(ctx):
    return ctx.send.await_args.kwargs["embed"]


class TestArqPoolUnavailable:
    async def test_arq_pool_none_returns_service_unavailable(
        self,
        mock_bot,
        mock_context,
    ):
        mock_bot.arq_pool = None
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_context.defer.assert_awaited_once()
        mock_context.send.assert_awaited_once()
        assert _last_embed(mock_context).title == "Service unavailable"

    async def test_enqueue_raises_returns_service_unavailable(
        self,
        mock_bot,
        mock_context,
    ):
        mock_bot.arq_pool.enqueue_job = AsyncMock(
            side_effect=RuntimeError("redis gone")
        )
        cog = Download(mock_bot)

        await _invoke(cog, mock_context)

        mock_bot.logger.exception.assert_called_once()
        assert _last_embed(mock_context).title == "Service unavailable"


class TestEnqueueHappyPath:
    async def test_enqueues_with_payload_and_acks_blurple(
        self,
        mock_bot,
        mock_context,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, only_me=False)

        mock_bot.arq_pool.enqueue_job.assert_awaited_once()
        call = mock_bot.arq_pool.enqueue_job.await_args
        assert call.args[0] == "download_channel_media"
        payload = call.args[1]
        assert payload["channel_id"] == 555
        assert payload["guild_id"] == 12345
        assert payload["requester_id"] == 42
        assert payload["only_me"] is False
        assert payload["allowed_media_types"] == [
            "image/png",
            "image/jpeg",
            "video/mp4",
        ]
        # Job id passed as keyword argument and matches the payload field.
        assert call.kwargs["_job_id"] == payload["job_id"]

        assert _last_embed(mock_context).title == "Download queued"


class TestOnlyMe:
    async def test_only_me_propagates_into_payload_and_ephemeral_flags(
        self,
        mock_bot,
        mock_context,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, mock_context, only_me=True)

        # Defer was called with ephemeral=True
        assert mock_context.defer.await_args.kwargs == {"ephemeral": True}
        # Payload propagates only_me
        payload = mock_bot.arq_pool.enqueue_job.await_args.args[1]
        assert payload["only_me"] is True
        # Ack send is also ephemeral
        assert mock_context.send.await_args.kwargs["ephemeral"] is True


class TestDmContext:
    async def test_dm_context_payload_guild_id_is_none(
        self,
        mock_bot,
        dm_context,
    ):
        cog = Download(mock_bot)

        await _invoke(cog, dm_context)

        payload = mock_bot.arq_pool.enqueue_job.await_args.args[1]
        assert payload["guild_id"] is None
