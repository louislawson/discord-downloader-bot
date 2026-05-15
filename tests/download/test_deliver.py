"""Branch tests for ``downloader_bot.download.deliver`` — DM and channel-post functions.

The arq predecessor had a single ``deliver(...)`` with all the routing
logic baked in, plus Redis idempotency. In the Taskiq version that's
split: ``dm_user``/``post_to_channel`` are pure-Discord; idempotency
lives in ``app/download/idempotency.py`` (tested separately) and routing
lives in the orchestrator (tested in ``tests/tasks/test_download.py``).
"""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from downloader_bot.download.deliver import DMUnavailable, dm_user, post_to_channel


class TestDmUser:
    async def test_sends_archive_url_via_dm(self, mock_discord_client, user_mock):
        mock_discord_client.fetch_user.return_value = user_mock

        await dm_user(mock_discord_client, 42, "https://x/signed?sas")

        mock_discord_client.fetch_user.assert_awaited_once_with(42)
        user_mock.send.assert_awaited_once()
        # URL appears verbatim in the embed description.
        embed = user_mock.send.await_args.kwargs["embed"]
        assert "https://x/signed?sas" in embed.description

    async def test_forbidden_raises_dm_unavailable(
        self,
        mock_discord_client,
        user_mock,
        forbidden_factory,
    ):
        # DMs disabled / bot blocked. The wrapper translates the
        # framework exception to a domain-typed DMUnavailable so the
        # orchestrator can branch on it without importing discord.
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock

        with pytest.raises(DMUnavailable, match="cannot DM user 42"):
            await dm_user(mock_discord_client, 42, "https://x")

    async def test_non_forbidden_http_exception_propagates_unwrapped(
        self,
        mock_discord_client,
        user_mock,
    ):
        # Transient 5xx should NOT be wrapped — retry middleware /
        # orchestrator can decide what to do.
        response = MagicMock(status=500, reason="Internal Server Error")
        user_mock.send = AsyncMock(side_effect=discord.HTTPException(response, "boom"))
        mock_discord_client.fetch_user.return_value = user_mock

        with pytest.raises(discord.HTTPException):
            await dm_user(mock_discord_client, 42, "https://x")


class TestPostToChannel:
    async def test_posts_with_requester_mention(
        self,
        mock_discord_client,
        channel_mock,
    ):
        mock_discord_client.fetch_channel.return_value = channel_mock

        await post_to_channel(
            mock_discord_client,
            999,
            "https://x/signed",
            fallback_user_id=42,
        )

        mock_discord_client.fetch_channel.assert_awaited_once_with(999)
        channel_mock.send.assert_awaited_once()
        embed = channel_mock.send.await_args.kwargs["embed"]
        assert "<@42>" in embed.footer.text
        assert "https://x/signed" in embed.description

    async def test_non_messageable_falls_back_to_dm(
        self,
        mock_discord_client,
        user_mock,
    ):
        # fetch_channel returned a non-Messageable (e.g. CategoryChannel).
        not_messageable = MagicMock()  # no Messageable spec → isinstance False
        mock_discord_client.fetch_channel.return_value = not_messageable
        mock_discord_client.fetch_user.return_value = user_mock

        await post_to_channel(
            mock_discord_client,
            999,
            "https://x",
            fallback_user_id=42,
        )

        # Channel attempt was made but didn't send; DM took over.
        user_mock.send.assert_awaited_once()

    async def test_channel_not_found_falls_back_to_dm(
        self,
        mock_discord_client,
        user_mock,
        not_found_factory,
    ):
        mock_discord_client.fetch_channel = AsyncMock(side_effect=not_found_factory())
        mock_discord_client.fetch_user.return_value = user_mock

        await post_to_channel(
            mock_discord_client,
            999,
            "https://x",
            fallback_user_id=42,
        )

        user_mock.send.assert_awaited_once()

    async def test_channel_forbidden_falls_back_to_dm(
        self,
        mock_discord_client,
        user_mock,
        channel_mock,
        forbidden_factory,
    ):
        # Bot can fetch the channel but can't post in it (Send Messages
        # perm revoked). Fallback path mirrors the not-found case.
        channel_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_channel.return_value = channel_mock
        mock_discord_client.fetch_user.return_value = user_mock

        await post_to_channel(
            mock_discord_client,
            999,
            "https://x",
            fallback_user_id=42,
        )

        channel_mock.send.assert_awaited_once()
        user_mock.send.assert_awaited_once()

    async def test_dm_fallback_can_also_raise(
        self,
        mock_discord_client,
        user_mock,
        forbidden_factory,
        not_found_factory,
    ):
        # Both routes blocked: channel unreachable + DMs disabled. The
        # DMUnavailable raised by the fallback should propagate (the
        # orchestrator has no further fallback to offer).
        mock_discord_client.fetch_channel = AsyncMock(side_effect=not_found_factory())
        user_mock.send = AsyncMock(side_effect=forbidden_factory())
        mock_discord_client.fetch_user.return_value = user_mock

        with pytest.raises(DMUnavailable):
            await post_to_channel(
                mock_discord_client,
                999,
                "https://x",
                fallback_user_id=42,
            )
