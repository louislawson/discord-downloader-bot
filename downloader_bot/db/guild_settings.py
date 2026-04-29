"""Per-guild delivery settings backed by Postgres.

Reads return ``('dm', None)`` for unknown or DM-context guilds so unconfigured
servers still get a working delivery path without an explicit ``/setup`` run.
"""

from typing import Literal

import asyncpg

DeliveryMode = Literal["dm", "channel", "both"]


async def get(
    pool: asyncpg.Pool,
    guild_id: int | None,
) -> tuple[DeliveryMode, int | None]:
    """
    Read delivery settings for ``guild_id``.

    Returns ``('dm', None)`` when ``guild_id`` is ``None`` (DM-context
    invocation) or no row exists for the guild.
    """
    if guild_id is None:
        return ("dm", None)
    row = await pool.fetchrow(
        "SELECT delivery_mode, results_channel_id FROM guild_settings "
        "WHERE guild_id = $1",
        guild_id,
    )
    if row is None:
        return ("dm", None)
    return (row["delivery_mode"], row["results_channel_id"])


async def set_mode(
    pool: asyncpg.Pool,
    guild_id: int,
    mode: DeliveryMode,
) -> None:
    """Upsert ``delivery_mode`` for ``guild_id``; preserves any existing channel."""
    await pool.execute(
        "INSERT INTO guild_settings (guild_id, delivery_mode) "
        "VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE "
        "SET delivery_mode = EXCLUDED.delivery_mode, updated_at = now()",
        guild_id,
        mode,
    )


async def set_channel(
    pool: asyncpg.Pool,
    guild_id: int,
    channel_id: int,
) -> None:
    """Upsert ``results_channel_id`` for ``guild_id``; mode defaults to 'dm' on first insert."""
    await pool.execute(
        "INSERT INTO guild_settings (guild_id, results_channel_id) "
        "VALUES ($1, $2) "
        "ON CONFLICT (guild_id) DO UPDATE "
        "SET results_channel_id = EXCLUDED.results_channel_id, updated_at = now()",
        guild_id,
        channel_id,
    )


async def clear_channel(pool: asyncpg.Pool, guild_id: int) -> None:
    """Clear the configured results channel for ``guild_id`` (no-op if no row)."""
    await pool.execute(
        "UPDATE guild_settings "
        "SET results_channel_id = NULL, updated_at = now() "
        "WHERE guild_id = $1",
        guild_id,
    )
