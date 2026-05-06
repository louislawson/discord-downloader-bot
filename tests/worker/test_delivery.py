"""Branch tests for ``deliver()`` — DM/channel routing decision tree."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from downloader_bot.worker.delivery import DeliveryPayload, deliver


def _payload():
    """Minimal DeliveryPayload — embed only, no attachment."""
    return DeliveryPayload(embed=discord.Embed(title="Test"))


class TestIdempotency:
    async def test_skips_send_when_already_delivered(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
    ):
        # Marker present → the previous run completed; this run must not send.
        mock_redis.get = AsyncMock(return_value="1")

        await deliver(
            mock_discord_client,
            mock_redis,
            mock_db_pool,
            "job-1",
            requester_id=42,
            guild_id=None,
            only_me=True,
            payload=_payload(),
        )

        mock_discord_client.fetch_user.assert_not_awaited()
        mock_discord_client.fetch_channel.assert_not_awaited()
        # Don't write the marker again — preserves the original TTL.
        mock_redis.set.assert_not_awaited()

    async def test_marks_delivered_only_after_successful_send(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
        user_mock,
    ):
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            mock_db_pool,
            "job-2",
            42,
            guild_id=None,
            only_me=True,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        mock_redis.set.assert_awaited_once()
        key = mock_redis.set.await_args.args[0]
        assert key == "delivered:job-2"

    async def test_does_not_mark_delivered_on_transient_send_failure(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
        user_mock,
    ):
        # Non-Forbidden Discord error mid-send: the wrapper / a retry must
        # be free to re-attempt, so the marker must NOT be written.
        response = MagicMock(status=500, reason="Internal Server Error")
        user_mock.send = AsyncMock(side_effect=discord.HTTPException(response, "boom"))
        mock_discord_client.fetch_user.return_value = user_mock

        with pytest.raises(discord.HTTPException):
            await deliver(
                mock_discord_client,
                mock_redis,
                mock_db_pool,
                "job-3",
                42,
                guild_id=None,
                only_me=True,
                payload=_payload(),
            )

        user_mock.send.assert_awaited_once()
        mock_redis.set.assert_not_awaited()

    async def test_marks_delivered_on_fail_closed_forbidden(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
        user_mock,
        forbidden_factory,
    ):
        # Forbidden is terminal — a retry won't unblock the user's DMs, so
        # the marker IS written to prevent re-attempts within the TTL.
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            mock_db_pool,
            "job-4",
            42,
            guild_id=None,
            only_me=True,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        mock_redis.set.assert_awaited_once()


class TestOnlyMe:
    async def test_dm_success(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
        user_mock,
    ):
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            mock_db_pool,
            "job-2",
            42,
            guild_id=12345,
            only_me=True,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()

    async def test_dm_forbidden_fails_closed(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
        user_mock,
        forbidden_factory,
    ):
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            mock_db_pool,
            "job-3",
            42,
            guild_id=12345,
            only_me=True,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        mock_discord_client.fetch_channel.assert_not_awaited()


class TestModeDm:
    async def test_dm_success(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
    ):
        pool = make_db_pool(mode="dm")
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-4",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()


class TestModeChannel:
    async def test_posts_to_configured_channel_with_requester_mention(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        channel_mock,
    ):
        pool = make_db_pool(mode="channel", channel_id=999)
        mock_discord_client.fetch_channel.return_value = channel_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-5",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        channel_mock.send.assert_awaited_once()
        kwargs = channel_mock.send.await_args.kwargs
        assert kwargs["content"] == "<@42>"

    async def test_falls_back_to_dm_when_no_channel_configured(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
    ):
        pool = make_db_pool(mode="channel", channel_id=None)
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-6",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        mock_discord_client.fetch_channel.assert_not_awaited()

    async def test_channel_post_forbidden_falls_back_to_dm_fail_closed(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
        channel_mock,
        forbidden_factory,
    ):
        pool = make_db_pool(mode="channel", channel_id=999)
        channel_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_channel.return_value = channel_mock
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-channel-forbidden",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        channel_mock.send.assert_awaited_once()
        # Failure must fall back to DM rather than propagating to the wrapper.
        user_mock.send.assert_awaited_once()
        # Successful fallback DM is treated as a completed attempt.
        mock_redis.set.assert_awaited_once()

    async def test_channel_not_found_falls_back_to_dm(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
    ):
        pool = make_db_pool(mode="channel", channel_id=999)
        response = MagicMock(status=404, reason="Not Found")
        mock_discord_client.fetch_channel = AsyncMock(
            side_effect=discord.NotFound(response, "deleted")
        )
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-channel-not-found",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        # Channel lookup raised; DM was attempted as fallback.
        user_mock.send.assert_awaited_once()


class TestModeBoth:
    async def test_dm_succeeds_skips_channel_post(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
        channel_mock,
    ):
        pool = make_db_pool(mode="both", channel_id=999)
        mock_discord_client.fetch_user.return_value = user_mock
        mock_discord_client.fetch_channel.return_value = channel_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-7",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        channel_mock.send.assert_not_awaited()

    async def test_dm_forbidden_falls_back_to_channel(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
        channel_mock,
        forbidden_factory,
    ):
        pool = make_db_pool(mode="both", channel_id=999)
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock
        mock_discord_client.fetch_channel.return_value = channel_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-8",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        channel_mock.send.assert_awaited_once()

    async def test_dm_forbidden_no_channel_drops_silently(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
        forbidden_factory,
    ):
        pool = make_db_pool(mode="both", channel_id=None)
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock

        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-9",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        mock_discord_client.fetch_channel.assert_not_awaited()

    async def test_dm_blocked_and_channel_post_forbidden_drops_silently(
        self,
        mock_discord_client,
        mock_redis,
        make_db_pool,
        user_mock,
        channel_mock,
        forbidden_factory,
    ):
        pool = make_db_pool(mode="both", channel_id=999)
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        channel_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock
        mock_discord_client.fetch_channel.return_value = channel_mock

        # Both routes blocked; deliver must complete without raising.
        await deliver(
            mock_discord_client,
            mock_redis,
            pool,
            "job-both-blocked",
            42,
            guild_id=12345,
            only_me=False,
            payload=_payload(),
        )

        user_mock.send.assert_awaited_once()
        channel_mock.send.assert_awaited_once()
        # Both routes attempted = a completed attempt; the marker IS written
        # to prevent re-attempts within the TTL.
        mock_redis.set.assert_awaited_once()
