"""asyncpg pool factory + schema bootstrap.

The schema is applied idempotently on pool startup so dev and CI don't
need a separate migration step. Production should still own migrations
explicitly (Alembic or similar) once the schema starts changing.
"""

from pathlib import Path

import asyncpg

from downloader_bot.config import settings

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"


async def build_pool() -> asyncpg.Pool:
    """Open the asyncpg pool and apply ``schema.sql`` idempotently.

    The schema is applied on every startup so dev / CI don't need a
    separate migration step. Both the bot and the worker call this
    concurrently — that's safe because every statement in ``schema.sql``
    is ``IF NOT EXISTS`` or equivalent.

    Returns:
        A ready-to-use ``asyncpg.Pool`` (1-10 connections).

    Raises:
        asyncpg.exceptions.PostgresError: Pool creation or schema apply
            failed.
    """
    pool = await asyncpg.create_pool(
        dsn=settings.POSTGRES_DSN,
        min_size=1,
        max_size=10,
    )
    assert (
        pool is not None
    )  # asyncpg returns Optional in typeshed; never None in practice
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL.read_text())
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    """Release pool connections; call at process shutdown.

    Args:
        pool: The pool returned by :func:`build_pool`.
    """
    await pool.close()
