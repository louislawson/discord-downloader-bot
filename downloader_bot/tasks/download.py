"""Taskiq orchestrator for the channel-media download pipeline."""

import logging
import re
from datetime import date, timedelta
from typing import Annotated, TypedDict

import aiohttp
import discord
from redis.asyncio import Redis
from taskiq import Context, TaskiqDepends
from taskiq.depends.progress_tracker import ProgressTracker, TaskState

from downloader_bot.db.guild_settings import GuildSettings, GuildSettingsRepo
from downloader_bot.download import deliver, idempotency, zip_stream
from downloader_bot.storage.base import StorageBackend
from downloader_bot.storage.exceptions import UploadError
from downloader_bot.tq import (
    broker,
    cancellation_backend,
    get_discord_client,
    get_download_session,
    get_guild_settings_repo,
    get_redis,
    get_storage,
)

logger = logging.getLogger(__name__)


class DownloadResult(TypedDict):
    """Return shape of ``download_channel_media``.

    A real type lets the cog (and any future ``await task.wait_result()``
    caller) type-check the URL access without resorting to dict[str, Any]
    introspection.
    """

    url: str
    delivery_mode: str  # 'dm' or 'channel'


def _display_filename(channel: discord.abc.Messageable) -> str:
    """Human-facing zip filename like ``channel-general-2026-05-09.zip``.

    Falls back to ``id-<n>`` for DMs and channels without a name. Strips
    path separators and shell-grief characters.
    """
    raw_name = getattr(channel, "name", None) or f"id-{channel.id}"
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', "-", raw_name).strip("-")
    return f"channel-{safe_name or 'unnamed'}-{date.today().isoformat()}.zip"


@broker.task(
    task_name="download_channel_media",
    retry_on_error=True,
    max_retries=3,
)
@cancellation_backend.cancellable
async def download_channel_media(
    channel_id: int,
    user_id: int,
    guild_id: int | None,
    only_me: bool,
    context: Annotated[Context, TaskiqDepends()],
    progress: Annotated[ProgressTracker, TaskiqDepends()],
    client: Annotated[discord.Client, TaskiqDepends(get_discord_client)],
    download_session: Annotated[
        aiohttp.ClientSession,
        TaskiqDepends(get_download_session),
    ],
    storage: Annotated[StorageBackend, TaskiqDepends(get_storage)],
    settings_repo: Annotated[
        GuildSettingsRepo,
        TaskiqDepends(get_guild_settings_repo),
    ],
    redis: Annotated[Redis, TaskiqDepends(get_redis)],
) -> DownloadResult:
    task_id = context.message.task_id

    # Resolve effective delivery mode: only_me forces DM, otherwise honour
    # guild settings. Guilds without a row get safe defaults (delivery_mode='dm').
    if guild_id is not None and not only_me:
        guild_settings: GuildSettings = await settings_repo.get(guild_id)
    else:
        # DM channel or only_me override — no guild lookup needed.
        guild_settings = GuildSettings(guild_id=guild_id or 0)
    effective_delivery_mode = "dm" if only_me else guild_settings.delivery_mode
    ttl = timedelta(hours=guild_settings.retention_hours)
    ttl_seconds = int(ttl.total_seconds())

    # Phase 1 — upload. Skip if a prior retry attempt cached the URL.
    archive_url = await idempotency.get_cached_archive_url(redis, task_id)

    if archive_url is None:
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_id} is not messageable")

        key = f"channel-{channel_id}-{task_id}.zip"

        await progress.set_progress(
            state=TaskState.STARTED,
            meta={"phase": "stream", "key": key},
        )

        stream = zip_stream.build_zip_stream(
            download_session,
            channel,
            # Apply the guild's allowed_media_types filter. None = accept all.
            allowed_types=set(guild_settings.allowed_media_types)
            if guild_settings.allowed_media_types is not None
            else None,
        )

        # try/finally with an explicit success flag is the Pythonic shape
        # for "clean up the partial blob on any non-success path, including
        # cancellation". Catching BaseException would do the same job but
        # violates PEP 8's "catch specific exceptions" rule and is uglier.
        # On cancellation, finally still runs and the CancelledError
        # propagates naturally afterwards.
        upload_succeeded = False
        try:
            archive_url = await storage.upload_and_sign(
                name=key,
                data=stream,
                download_filename=_display_filename(channel),
            )
            upload_succeeded = True
        finally:
            if not upload_succeeded:
                try:
                    await storage.delete_blob(key)
                except UploadError as exc:
                    # Best-effort cleanup. UploadError is delete_blob's only
                    # documented exception (per app/storage/azure.py); other
                    # exception types here imply programming bugs and should
                    # surface, not be silently swallowed.
                    logger.warning(
                        "best-effort delete of partial blob '%s' failed: %s",
                        key,
                        exc,
                    )

        # Mark upload complete only after the URL is provably good. A
        # failure here means retry re-uploads (wasteful but correct).
        await idempotency.cache_archive_url(
            redis,
            task_id,
            archive_url,
            ttl_seconds,
        )
    else:
        logger.info(
            "Retry of task %s: skipping upload, reusing cached URL",
            task_id,
        )

    # Phase 2 — deliver. Skip if a prior retry attempt already notified.
    if await idempotency.is_delivered(redis, task_id):
        logger.info(
            "Retry of task %s: skipping delivery, already delivered",
            task_id,
        )
    else:
        await progress.set_progress(
            state=TaskState.STARTED,
            meta={"phase": "deliver"},
        )
        if (
            effective_delivery_mode == "channel"
            and guild_settings.results_channel_id is not None
        ):
            await deliver.post_to_channel(
                client,
                guild_settings.results_channel_id,
                archive_url,
                fallback_user_id=user_id,
            )
        else:
            await deliver.dm_user(client, user_id, archive_url)
        # Mark AFTER the send returns. A crash here means retry re-delivers
        # — duplicate DM beats no DM.
        await idempotency.mark_delivered(redis, task_id, ttl_seconds)

    await progress.set_progress(state=TaskState.SUCCESS, meta={"phase": "done"})
    return {"url": archive_url, "delivery_mode": effective_delivery_mode}
