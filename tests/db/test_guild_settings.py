"""Unit tests for guild_settings — Postgres-backed delivery preferences."""

from downloader_bot.db import guild_settings


class TestGet:
    async def test_returns_default_when_guild_id_is_none(self, mock_db_pool):
        result = await guild_settings.get(mock_db_pool, None)

        assert result == ("dm", None)
        mock_db_pool.fetchrow.assert_not_awaited()

    async def test_returns_default_when_no_row_present(self, make_db_pool):
        pool = make_db_pool(row_present=False)

        result = await guild_settings.get(pool, 12345)

        assert result == ("dm", None)
        pool.fetchrow.assert_awaited_once()

    async def test_returns_row_values_when_present(self, make_db_pool):
        pool = make_db_pool(mode="channel", channel_id=999)

        result = await guild_settings.get(pool, 12345)

        assert result == ("channel", 999)


class TestSetters:
    async def test_set_mode_upserts(self, mock_db_pool):
        await guild_settings.set_mode(mock_db_pool, 12345, "both")

        mock_db_pool.execute.assert_awaited_once()
        sql = mock_db_pool.execute.await_args.args[0]
        assert "INSERT INTO guild_settings" in sql
        assert "ON CONFLICT (guild_id) DO UPDATE" in sql
        assert "delivery_mode" in sql
        assert mock_db_pool.execute.await_args.args[1:] == (12345, "both")

    async def test_set_channel_upserts(self, mock_db_pool):
        await guild_settings.set_channel(mock_db_pool, 12345, 999)

        mock_db_pool.execute.assert_awaited_once()
        sql = mock_db_pool.execute.await_args.args[0]
        assert "INSERT INTO guild_settings" in sql
        assert "ON CONFLICT (guild_id) DO UPDATE" in sql
        assert "results_channel_id" in sql
        assert mock_db_pool.execute.await_args.args[1:] == (12345, 999)

    async def test_clear_channel_updates_to_null(self, mock_db_pool):
        await guild_settings.clear_channel(mock_db_pool, 12345)

        mock_db_pool.execute.assert_awaited_once()
        sql = mock_db_pool.execute.await_args.args[0]
        assert "UPDATE guild_settings" in sql
        assert "results_channel_id = NULL" in sql
        assert mock_db_pool.execute.await_args.args[1:] == (12345,)
