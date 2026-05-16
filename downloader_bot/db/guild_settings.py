"""GuildSettings dataclass + GuildSettingsRepo.

Repository pattern: one class owns CRUD, no SQL leaks into the
orchestrator. Defaults are baked into the dataclass — a guild without
a row gets a default object without raising.
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg


@dataclass(frozen=True, slots=True)
class GuildSettings:
    """Per-guild configuration row, with safe defaults baked in.

    Constructing ``GuildSettings(guild_id=N)`` without DB-row defaults is
    the canonical "unconfigured guild" object: ``delivery_mode='dm'``,
    no results channel, no media-type filter, 24h retention. The repo's
    :meth:`GuildSettingsRepo.get` returns this rather than ``None`` on a
    missing row, so callers never need to special-case absence.
    """

    guild_id: int
    delivery_mode: str = "dm"  # 'dm' | 'channel'
    results_channel_id: int | None = None
    allowed_media_types: list[str] | None = None  # None = all
    max_archive_size_bytes: int | None = None  # None = no cap
    retention_hours: int = 24
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GuildSettingsRepo:
    """CRUD for guild_settings. Constructed once per worker / bot process."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind the repo to a long-lived asyncpg pool.

        Args:
            pool: Process-wide asyncpg pool; the repo only borrows
                connections from it.
        """
        self._pool = pool

    async def get(self, guild_id: int) -> GuildSettings:
        """Return settings for ``guild_id``, with defaults for a missing row.

        Args:
            guild_id: The Discord guild ID to look up.

        Returns:
            A ``GuildSettings`` populated from the database, or a default
            ``GuildSettings(guild_id=guild_id)`` if no row exists.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM guild_settings WHERE guild_id = $1",
                guild_id,
            )
        if row is None:
            return GuildSettings(guild_id=guild_id)
        return GuildSettings(
            guild_id=row["guild_id"],
            delivery_mode=row["delivery_mode"],
            results_channel_id=row["results_channel_id"],
            allowed_media_types=list(row["allowed_media_types"])
            if row["allowed_media_types"] is not None
            else None,
            max_archive_size_bytes=row["max_archive_size_bytes"],
            retention_hours=row["retention_hours"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def upsert(self, settings: GuildSettings) -> None:
        """Insert or replace the row for ``settings.guild_id``.

        This is a single-shot overwrite (every non-PK column is written),
        not a read-modify-write. Callers wanting to preserve untouched
        columns must read first and pass a ``replace(...)``-modified copy
        — see :meth:`Setup.setup_clear` in [cogs/setup.py](../cogs/setup.py).

        Args:
            settings: The full target row state.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, delivery_mode, results_channel_id,
                    allowed_media_types, max_archive_size_bytes, retention_hours
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id) DO UPDATE SET
                    delivery_mode = EXCLUDED.delivery_mode,
                    results_channel_id = EXCLUDED.results_channel_id,
                    allowed_media_types = EXCLUDED.allowed_media_types,
                    max_archive_size_bytes = EXCLUDED.max_archive_size_bytes,
                    retention_hours = EXCLUDED.retention_hours,
                    updated_at = now()
                """,
                settings.guild_id,
                settings.delivery_mode,
                settings.results_channel_id,
                settings.allowed_media_types,
                settings.max_archive_size_bytes,
                settings.retention_hours,
            )
