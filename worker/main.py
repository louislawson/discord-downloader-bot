"""ARQ worker entrypoint.

Run with ``arq worker.main.WorkerSettings`` from the project root. The worker
connects to Redis using the same ``REDIS_URL`` the bot uses to enqueue jobs.

``on_startup`` opens a single REST-only Discord client and a Postgres pool and
stashes them on the ARQ context as ``ctx['discord_client']`` and
``ctx['db_pool']`` so every job reuses the same logged-in HTTP session and
database connections. ``on_shutdown`` closes them on the way out.
"""

import logging
from typing import Any

import discord

from config import settings
from db.pool import open_pool as open_db_pool
from queue_client import redis_settings
from worker.discord_rest import open_client
from worker.jobs import download_channel_media


logger = logging.getLogger("downloader_bot.worker")
logger.setLevel(settings.LOGGING_LEVEL)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    logger.addHandler(handler)


async def noop_job(ctx: dict, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Smoke-test job — logs the payload and echoes it back.

    Kept around so ``<PREFIX>queueping`` can verify the bot↔worker round trip
    without exercising the full download path.

    Args:
        ctx (dict): ARQ-supplied context (job metadata, redis handle, etc.).
        payload (dict): Arbitrary dict supplied by the caller.

    Returns:
        dict: ``{"ok": True, "echo": payload}``.
    """
    logger.info(
        "noop_job received (job_id=%s, try=%s): %s",
        ctx.get("job_id"),
        ctx.get("job_try"),
        payload,
    )
    return {"ok": True, "echo": payload}


async def on_startup(ctx: dict) -> None:
    """
    Worker lifecycle hook — runs once when the worker process starts.

    Opens a REST-only Discord client (no gateway) and a Postgres pool, both
    shared by every job in this worker, stored as ``ctx['discord_client']``
    and ``ctx['db_pool']``.
    """
    logger.info("Worker starting up (REDIS_URL=%s)", settings.REDIS_URL)
    ctx["discord_client"] = await open_client(settings.TOKEN)
    logger.info("REST-only Discord client logged in")
    ctx["db_pool"] = await open_db_pool()
    logger.info("Connected to Postgres")


async def on_shutdown(ctx: dict) -> None:
    """Worker lifecycle hook — runs once when the worker process stops."""
    client: discord.Client | None = ctx.get("discord_client")
    if client is not None:
        await client.close()
        logger.info("REST-only Discord client closed")
    db_pool = ctx.get("db_pool")
    if db_pool is not None:
        await db_pool.close()
        logger.info("Postgres pool closed")
    logger.info("Worker shutting down")


class WorkerSettings:
    """ARQ worker configuration. ``arq`` discovers this class by import path."""

    functions = [noop_job, download_channel_media]
    redis_settings = redis_settings()
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_jobs = 3
    job_timeout = 1800  # 30 minutes — generous for big-channel zips
    # ARQ only honours ``max_tries`` for ``Retry``/``RetryJob``-driven
    # retries. Arbitrary unhandled exceptions fail the job immediately
    # regardless of this value — see ``download_channel_media``'s wrapper
    # for how that's surfaced to the user.
    max_tries = 2
