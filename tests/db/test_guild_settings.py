"""Unit tests for ``downloader_bot.db.guild_settings`` — repo + dataclass.

The repo is a thin asyncpg wrapper; tests mock the connection so they
run without a real Postgres. The dataclass defaults are part of the
contract (``get`` returns them when no row exists) and are pinned here.
"""

import pytest

from downloader_bot.db.guild_settings import GuildSettings, GuildSettingsRepo

# --- GuildSettings dataclass ----------------------------------------------


class TestGuildSettingsDefaults:
    def test_defaults_match_safe_baseline(self):
        # delivery_mode='dm' is the privacy-preserving default: a guild
        # that hasn't run /setup gets DM delivery, not channel-post.
        s = GuildSettings(guild_id=12345)

        assert s.delivery_mode == "dm"
        assert s.results_channel_id is None
        assert s.allowed_media_types is None  # None = all
        assert s.max_archive_size_bytes is None  # None = no cap
        assert s.retention_hours == 24
        assert s.created_at is None
        assert s.updated_at is None

    def test_frozen(self):
        s = GuildSettings(guild_id=12345)

        # frozen=True / slots=True — mutation should fail.
        with pytest.raises((AttributeError, TypeError)):
            s.delivery_mode = "channel"  # type: ignore[misc]


# --- GuildSettingsRepo.get ------------------------------------------------


class TestRepoGet:
    async def test_returns_defaults_when_no_row(self, make_db_pool):
        pool = make_db_pool(row_present=False)
        repo = GuildSettingsRepo(pool)

        result = await repo.get(12345)

        # No row → defaults. The repo does NOT insert a default row
        # implicitly; guilds only appear in the table after /setup.
        assert result == GuildSettings(guild_id=12345)
        pool._conn.execute.assert_not_awaited()

    async def test_returns_row_when_present(self, make_db_pool):
        pool = make_db_pool(
            mode="channel",
            channel_id=999,
            allowed_media_types=["image/png", "video/mp4"],
            retention_hours=48,
        )
        repo = GuildSettingsRepo(pool)

        result = await repo.get(12345)

        assert result.delivery_mode == "channel"
        assert result.results_channel_id == 999
        assert result.allowed_media_types == ["image/png", "video/mp4"]
        assert result.retention_hours == 48

    async def test_uses_select_with_guild_id(self, make_db_pool):
        pool = make_db_pool()
        repo = GuildSettingsRepo(pool)

        await repo.get(12345)

        sql = pool._conn.fetchrow.await_args.args[0]
        assert "SELECT" in sql
        assert "WHERE guild_id = $1" in sql
        assert pool._conn.fetchrow.await_args.args[1] == 12345

    async def test_handles_null_allowed_media_types(self, make_db_pool):
        # Postgres NULL → Python None on the dataclass.
        pool = make_db_pool(allowed_media_types=None)
        repo = GuildSettingsRepo(pool)

        result = await repo.get(12345)

        assert result.allowed_media_types is None


# --- GuildSettingsRepo.upsert ---------------------------------------------


class TestRepoUpsert:
    async def test_upsert_includes_all_fields(self, make_db_pool):
        pool = make_db_pool()
        repo = GuildSettingsRepo(pool)
        settings_obj = GuildSettings(
            guild_id=12345,
            delivery_mode="channel",
            results_channel_id=999,
            allowed_media_types=["image/png"],
            max_archive_size_bytes=10_000_000,
            retention_hours=12,
        )

        await repo.upsert(settings_obj)

        pool._conn.execute.assert_awaited_once()
        sql = pool._conn.execute.await_args.args[0]
        assert "INSERT INTO guild_settings" in sql
        assert "ON CONFLICT (guild_id) DO UPDATE" in sql
        # Args after the SQL string are the bind values, in column order.
        bind_args = pool._conn.execute.await_args.args[1:]
        assert bind_args == (
            12345,
            "channel",
            999,
            ["image/png"],
            10_000_000,
            12,
        )

    async def test_upsert_updates_updated_at_via_now(self, make_db_pool):
        pool = make_db_pool()
        repo = GuildSettingsRepo(pool)

        await repo.upsert(GuildSettings(guild_id=12345))

        sql = pool._conn.execute.await_args.args[0]
        # Conflict path bumps updated_at = now() so we know when a guild's
        # config last changed.
        assert "updated_at = now()" in sql

    async def test_dm_mode_with_no_channel_is_valid(self, make_db_pool):
        # The CHECK constraint forbids delivery_mode='channel' without a
        # results_channel_id, but 'dm' + None is fine.
        pool = make_db_pool()
        repo = GuildSettingsRepo(pool)

        await repo.upsert(
            GuildSettings(guild_id=12345, delivery_mode="dm", results_channel_id=None)
        )

        bind_args = pool._conn.execute.await_args.args[1:]
        assert bind_args[1] == "dm"
        assert bind_args[2] is None  # results_channel_id
