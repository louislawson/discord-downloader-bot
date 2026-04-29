"""Branch tests for ``deliver()`` — DM/channel routing decision tree."""

from unittest.mock import AsyncMock

import discord

from downloader_bot.worker.delivery import DeliveryPayload, deliver


def _payload():
    """Minimal DeliveryPayload — embed only, no attachment."""
    return DeliveryPayload(embed=discord.Embed(title="Test"))


class TestIdempotency:
    async def test_skips_send_when_claim_already_held(
        self,
        mock_discord_client,
        mock_redis,
        mock_db_pool,
    ):
        mock_redis.set = AsyncMock(return_value=False)

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
