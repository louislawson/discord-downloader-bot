"""Postgres connection pool shared by the bot and worker."""

from pathlib import Path

import asyncpg

from config import settings


_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def open_pool() -> asyncpg.Pool:
    """Open an ``asyncpg`` pool to the configured Postgres instance."""
    return await asyncpg.create_pool(dsn=settings.POSTGRES_DSN)


async def init_schema(pool: asyncpg.Pool) -> None:
    """
    Apply ``schema.sql`` idempotently.

    Safe to call on every bot startup — the DDL uses ``CREATE TABLE IF NOT
    EXISTS``. Avoids the overhead of a migration tool while there's only one
    table to manage.
    """
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
