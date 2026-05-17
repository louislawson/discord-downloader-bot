"""Branch tests for the worker's REST-only Discord client lifecycle."""

from unittest.mock import AsyncMock, MagicMock

import discord

from downloader_bot.download.discord_rest import close_client, open_client


class TestOpenClient:
    async def test_constructs_client_with_no_intents_and_logs_in(self, mocker):
        # Patch the Client *constructor* so we can assert intents=none() was
        # passed and so we get a mock back whose .login is an AsyncMock.
        fake_client = MagicMock()
        fake_client.login = AsyncMock()
        client_cls = mocker.patch(
            "downloader_bot.download.discord_rest.discord.Client",
            return_value=fake_client,
        )

        returned = await open_client("test-token")

        # Intents.none() — the worker never opens a gateway, so it has no
        # need for any intent flags.
        kwargs = client_cls.call_args.kwargs
        assert isinstance(kwargs["intents"], discord.Intents)
        assert kwargs["intents"].value == discord.Intents.none().value
        fake_client.login.assert_awaited_once_with("test-token")
        assert returned is fake_client


class TestCloseClient:
    async def test_close_delegates_to_client_close(self):
        client = MagicMock()
        client.close = AsyncMock()

        await close_client(client)

        client.close.assert_awaited_once_with()
