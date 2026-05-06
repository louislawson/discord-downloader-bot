"""Tests for the worker lifecycle hooks (``on_startup`` / ``on_shutdown``).

Every download job depends on the resources these hooks populate on the ARQ
context, so a regression here would silently break every job in production.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

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

    async def test_db_pool_failure_closes_discord_client_and_reraises(self, mocker):
        # ARQ does not call on_shutdown when on_startup raises, so a leaked
        # Discord client would persist for the worker process's lifetime.
        discord_client = AsyncMock()

        mocker.patch(
            "downloader_bot.worker.main.open_client",
            new_callable=AsyncMock,
            return_value=discord_client,
        )
        mocker.patch(
            "downloader_bot.worker.main.open_db_pool",
            new_callable=AsyncMock,
            side_effect=RuntimeError("postgres unreachable"),
        )

        ctx: dict = {}
        with pytest.raises(RuntimeError, match="postgres unreachable"):
            await main.on_startup(ctx)

        discord_client.close.assert_awaited_once()
        # ctx must not retain the half-opened resource.
        assert "discord_client" not in ctx

    async def test_http_session_construction_failure_cleans_up_predecessors(
        self, mocker
    ):
        discord_client = AsyncMock()
        db_pool = AsyncMock()

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
            side_effect=RuntimeError("connector setup blew up"),
        )

        ctx: dict = {}
        with pytest.raises(RuntimeError, match="connector setup blew up"):
            await main.on_startup(ctx)

        discord_client.close.assert_awaited_once()
        db_pool.close.assert_awaited_once()
        assert "discord_client" not in ctx
        assert "db_pool" not in ctx


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
