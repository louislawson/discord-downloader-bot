"""ARQ worker entrypoint.

Run with ``arq downloader_bot.worker.main.WorkerSettings`` from the project root. The worker
connects to Redis using the same ``REDIS_URL`` the bot uses to enqueue jobs.

``on_startup`` opens a single REST-only Discord client, a Postgres pool, and a
shared ``aiohttp.ClientSession`` for streaming attachment downloads from
Discord's CDN, then stashes them on the ARQ context as ``ctx['discord_client']``,
``ctx['db_pool']``, and ``ctx['http']`` so every job reuses them.
``on_shutdown`` closes them on the way out.

The ``aiohttp.ClientSession`` is owned by the worker rather than reusing
discord.py's internal session — that one is configured for Discord API
calls (token auth, rate limits) and is the wrong shape for raw CDN GETs.
"""

import logging
from typing import Any, ClassVar

import aiohttp
import discord

from downloader_bot.config import settings
from downloader_bot.db.pool import open_pool as open_db_pool
from downloader_bot.queue_client import redis_settings
from downloader_bot.worker.discord_rest import open_client
from downloader_bot.worker.jobs import download_channel_media

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

    Opens a REST-only Discord client (no gateway), a Postgres pool, and an
    aiohttp ``ClientSession`` for CDN downloads — all shared by every job in
    this worker and stored as ``ctx['discord_client']``, ``ctx['db_pool']``,
    and ``ctx['http']``.

    If any setup step raises, already-opened resources are cleaned up
    before the exception propagates. ARQ does not call ``on_shutdown``
    when ``on_startup`` raises, so any leak here would be permanent for
    the worker process's lifetime.
    """
    logger.info("Worker starting up (REDIS_URL=%s)", settings.REDIS_URL)
    try:
        ctx["discord_client"] = await open_client(settings.TOKEN)
        logger.info("REST-only Discord client logged in")
        ctx["db_pool"] = await open_db_pool()
        logger.info("Connected to Postgres")
        ctx["http"] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=1800, sock_read=60),
            connector=aiohttp.TCPConnector(limit=20, limit_per_host=10),
        )
        logger.info("Opened aiohttp ClientSession for CDN downloads")
    except Exception:
        logger.exception("Worker startup failed — cleaning up partial resources")
        await _close_partial_resources(ctx)
        raise


async def _close_partial_resources(ctx: dict) -> None:
    """Close whichever resources were opened before ``on_startup`` failed.

    Each close is wrapped so a cleanup error doesn't mask the original
    startup exception.
    """
    client = ctx.pop("discord_client", None)
    if client is not None:
        try:
            await client.close()
        except Exception:
            logger.exception("Error closing Discord client during startup cleanup")
    db_pool = ctx.pop("db_pool", None)
    if db_pool is not None:
        try:
            await db_pool.close()
        except Exception:
            logger.exception("Error closing Postgres pool during startup cleanup")
    http = ctx.pop("http", None)
    if http is not None:
        try:
            await http.close()
        except Exception:
            logger.exception("Error closing aiohttp session during startup cleanup")


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
    http: aiohttp.ClientSession | None = ctx.get("http")
    if http is not None:
        await http.close()
        logger.info("aiohttp ClientSession closed")
    logger.info("Worker shutting down")


class WorkerSettings:
    """ARQ worker configuration. ``arq`` discovers this class by import path."""

    functions: ClassVar = [noop_job, download_channel_media]
    redis_settings = redis_settings()
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_jobs = 3
    job_timeout = 1800  # 30 minutes — generous for big-channel zips
    # ``max_tries`` is intentionally omitted — ARQ only honours it for
    # ``Retry``/``RetryJob``-driven retries, and no worker code raises
    # either. Arbitrary exceptions fail the job permanently and are
    # surfaced to the user by ``download_channel_media``'s wrapper, which
    # is the actual retry-equivalent here. Setting ``max_tries`` to a
    # non-default value would only mislead a future reader.
    # Refresh the Redis health-check sentinel every 30 s (TTL = interval + 1)
    # so the per-service ``arq --check`` HEALTHCHECK in docker-compose.prod.yml
    # detects a dead worker within ~60-90 s. Default is 3600 s (1 h).
    health_check_interval = 30
