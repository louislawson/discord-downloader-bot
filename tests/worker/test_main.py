"""Tests for the worker lifecycle hooks (``on_startup`` / ``on_shutdown``).

Every download job depends on the resources these hooks populate on the ARQ
context, so a regression here would silently break every job in production.
"""

from unittest.mock import AsyncMock, MagicMock

from downloader_bot.worker import main


class TestOnStartup:
    async def test_populates_ctx_with_discord_client_db_pool_and_http(self, mocker):
        discord_client = AsyncMock()
        db_pool = AsyncMock()
        http_session = MagicMock()  # aiohttp.ClientSession() is sync-constructed

        mocker.patch(
            "downloader_bot.worker.main.open_client",
            new_callable=AsyncMock,
            return_value=discord_client,
        )
        mocker.patch(
            "downloader_bot.worker.main.open_db_pool",
            new_callable=AsyncMock,
            return_value=db_pool,
        )
        mocker.patch(
            "downloader_bot.worker.main.aiohttp.ClientSession",
            return_value=http_session,
        )

        ctx: dict = {}
        await main.on_startup(ctx)

        assert ctx["discord_client"] is discord_client
        assert ctx["db_pool"] is db_pool
        assert ctx["http"] is http_session


class TestOnShutdown:
    async def test_closes_all_three_resources(self):
        client = AsyncMock()
        db_pool = AsyncMock()
        http_session = AsyncMock()
        ctx = {
            "discord_client": client,
            "db_pool": db_pool,
            "http": http_session,
        }

        await main.on_shutdown(ctx)

        client.close.assert_awaited_once()
        db_pool.close.assert_awaited_once()
        http_session.close.assert_awaited_once()

    async def test_empty_ctx_does_not_raise(self):
        # Worker may have crashed during startup before any resource opened.
        await main.on_shutdown({})

    async def test_partial_ctx_closes_what_is_present(self):
        # E.g. Discord client opened, then Postgres failed before http session
        # was created. Shutdown should still close the resource that exists.
        client = AsyncMock()
        await main.on_shutdown({"discord_client": client})

        client.close.assert_awaited_once()
