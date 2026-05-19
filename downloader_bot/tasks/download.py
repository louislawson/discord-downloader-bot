"""Taskiq orchestrator for the channel-media download pipeline."""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, TypedDict

import aiohttp
import discord
from redis.asyncio import Redis
from taskiq import Context, TaskiqDepends
from taskiq.depends.progress_tracker import ProgressTracker, TaskState

from downloader_bot import embeds
from downloader_bot.db.guild_settings import GuildSettings, GuildSettingsRepo
from downloader_bot.download import (
    deliver,
    filters as filters_module,
    idempotency,
    jobs,
    zip_stream,
)
from downloader_bot.download.filters import DownloadFilters
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

# Sentinel cached under ``task:{id}:archive_url`` when a task short-circuited
# because the channel had nothing to archive. Distinguishes "empty" from
# "no prior attempt" (None) so a Taskiq retry skips the history walk.
_EMPTY_SENTINEL = ""


class DownloadResult(TypedDict):
    """Return shape of ``download_channel_media``.

    A real type lets the cog (and any future ``await task.wait_result()``
    caller) type-check the URL access without resorting to dict[str, Any]
    introspection.
    """

    url: str
    delivery_mode: str  # 'dm' or 'channel'
    status: Literal["delivered", "empty"]


class ProgressMeta(TypedDict, total=False):
    """Shape of the ``meta`` dict the task writes via ``progress.set_progress``.

    Colocated with the writer so the reader (the /status cog) imports
    one source of truth. ``total=False`` because each phase emits only
    the keys it cares about — ``picked_up`` carries just ``phase``;
    ``stream`` carries the full tally; ``deliver`` and ``done`` carry
    just ``phase``. ``history_fraction`` is ``None`` when the
    snowflake-bounds resolver failed (rare, fully-unbounded scan with
    a broken oldest-message lookup) — the cog skips the "Position"
    field in that case.
    """

    phase: Literal["picked_up", "stream", "deliver", "done"]
    attachments_done: int
    bytes_streamed: int
    history_fraction: float | None
    updated_at: str  # ISO timestamp; heartbeat


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
    dm_me: bool,
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
    filters: DownloadFilters | None = None,
) -> DownloadResult:
    """Stream a channel's attachments into a zip, upload, then deliver the URL.

    Two-phase pipeline, each phase guarded by a Redis idempotency key so a
    Taskiq retry (same ``task_id``) skips already-completed work:

    1. **Upload** — walk ``channel.history()`` (optionally bounded by
       ``filters.before`` / ``filters.after``), apply the resolved matcher
       (per-invocation filters intersected with the guild's
       ``allowed_media_types``), stream attachments through stream-zip into
       the storage backend, and cache the SAS URL. If the channel yields
       nothing to archive (empty, filtered out, all pre-flight failed),
       the upload short-circuits and an empty-string sentinel is cached
       so retries skip the history walk.
    2. **Deliver** — either DM the requester or post into the guild's
       configured results channel (with DM fallback). For the empty case
       the embed is the red "no attachments" notice; otherwise it's the
       green download-link embed.

    Effective delivery mode: ``dm_me=True`` forces DM; otherwise honour
    ``GuildSettings.delivery_mode`` (defaults to ``dm`` for unconfigured
    guilds).

    Args:
        channel_id: The Discord channel whose attachments are zipped.
        user_id: The requesting user; receives DMs, used as channel-post
            fallback, and rendered in the delivery embed footer.
        guild_id: The guild the command was invoked from, or ``None`` for
            DMs. Used to look up ``GuildSettings`` (delivery mode, retention,
            media-type filter); ``None`` skips the lookup and uses defaults.
        dm_me: When ``True``, forces DM delivery regardless of guild
            settings.
        context: Injected Taskiq context; ``context.message.task_id`` keys
            the idempotency entries.
        progress: Injected progress tracker; phase changes are published to
            the Taskiq admin UI.
        client: Injected REST-only discord.py client (worker-shared).
        download_session: Injected aiohttp session used by the zip pipeline
            (worker-shared, separate from discord.py's HTTP client).
        storage: Injected storage backend, kept warm for the worker's
            lifetime via an ``AsyncExitStack``.
        settings_repo: Injected per-guild settings repo.
        redis: Injected Redis client for idempotency state (app-namespaced,
            separate from the Taskiq result backend).
        filters: Per-invocation filter payload from the cog (category,
            author, date range). ``None`` means "no user filters — use the
            guild policy alone."

    Returns:
        The archive URL, the delivery mode actually used, and a status
        of ``"delivered"`` (success URL sent) or ``"empty"`` (no
        attachments found — error embed sent and ``url`` is empty).

    Raises:
        TypeError: ``channel_id`` resolved to a non-messageable resource.
        UploadError: The blob upload failed; the partial blob is best-effort
            deleted before the exception propagates.
        SignedUrlError: The upload succeeded but SAS signing failed.
        AttachmentStreamError: An attachment's HTTP body failed mid-stream.
        discord.Forbidden: The bot lacks ``Read Message History`` on the
            channel, or DMing the user is blocked.
        DMUnavailable: DM delivery raised ``Forbidden``.
    """
    task_id = context.message.task_id

    # First thing: emit the picked-up sentinel. The /cancel cog uses
    # "is_ready False AND get_progress None" to mean "still queued in
    # RabbitMQ" for the refund decision; emitting *any* progress here
    # makes that signal unambiguous (otherwise the cog would refund a
    # token even while the worker was already fetching the channel).
    await progress.set_progress(
        state=TaskState.STARTED,
        meta=ProgressMeta(phase="picked_up"),
    )

    # Resolve effective delivery mode: dm_me forces DM, otherwise honour
    # guild settings. Guilds without a row get safe defaults (delivery_mode='dm').
    if guild_id is not None and not dm_me:
        guild_settings: GuildSettings = await settings_repo.get(guild_id)
    else:
        # DM channel or dm_me override — no guild lookup needed.
        guild_settings = GuildSettings(guild_id=guild_id or 0)
    effective_delivery_mode = "dm" if dm_me else guild_settings.delivery_mode
    ttl = timedelta(hours=guild_settings.retention_hours)
    ttl_seconds = int(ttl.total_seconds())

    # Phase 1 — upload. Skip if a prior retry attempt cached the URL (or the
    # empty-channel sentinel).
    archive_url = await idempotency.get_cached_archive_url(redis, task_id)

    if archive_url is None:
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_id} is not messageable")

        key = f"channel-{channel_id}-{task_id}.zip"

        await progress.set_progress(
            state=TaskState.STARTED,
            meta=ProgressMeta(phase="stream"),
        )

        resolved = filters_module.resolve(
            filters,
            guild_settings,
            now=datetime.now(UTC),
        )

        async def _on_progress(snapshot: zip_stream.ProgressSnapshot) -> None:
            await progress.set_progress(
                state=TaskState.STARTED,
                meta=ProgressMeta(
                    phase="stream",
                    attachments_done=snapshot["attachments_done"],
                    bytes_streamed=snapshot["bytes_streamed"],
                    history_fraction=snapshot["history_fraction"],
                    updated_at=datetime.now(UTC).isoformat(),
                ),
            )

        stream = zip_stream.build_zip_stream(
            download_session,
            channel,
            matches=resolved.matches,
            before=resolved.before,
            after=resolved.after,
            on_progress=_on_progress,
        )

        # try/finally with an explicit success flag is the Pythonic shape
        # for "clean up the partial blob on any non-success path, including
        # cancellation". Catching BaseException would do the same job but
        # violates PEP 8's "catch specific exceptions" rule and is uglier.
        # On cancellation, finally still runs and the CancelledError
        # propagates naturally afterwards.
        upload_succeeded = False
        empty_channel = False
        try:
            try:
                archive_url = await storage.upload_and_sign(
                    name=key,
                    data=stream,
                    ttl=ttl,
                    download_filename=_display_filename(channel),
                )
            except zip_stream.NoMatchingAttachments:
                # Channel had nothing to archive. The peek wrapper raised
                # before stream-zip emitted any bytes, so the backend
                # should not have committed the blob — but we still run
                # best-effort cleanup below in case the SDK staged
                # anything (delete_blob swallows 404s). Stash a sentinel
                # so a Taskiq retry skips the history walk entirely.
                logger.info(
                    "Task %s: channel %s has no matching attachments; "
                    "short-circuiting upload",
                    task_id,
                    channel_id,
                )
                archive_url = _EMPTY_SENTINEL
                empty_channel = True
            upload_succeeded = True
        finally:
            if not upload_succeeded or empty_channel:
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

        # Mark upload phase complete (including the empty sentinel). A
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

    is_empty = archive_url == _EMPTY_SENTINEL

    # Phase 2 — deliver. Skip if a prior retry attempt already notified.
    if await idempotency.is_delivered(redis, task_id):
        logger.info(
            "Retry of task %s: skipping delivery, already delivered",
            task_id,
        )
    else:
        await progress.set_progress(
            state=TaskState.STARTED,
            meta=ProgressMeta(phase="deliver"),
        )
        embed = (
            embeds.no_attachments(requester_id=user_id)
            if is_empty
            else embeds.media_download(
                signed_url=archive_url,
                requester_id=user_id,
            )
        )
        if (
            effective_delivery_mode == "channel"
            and guild_settings.results_channel_id is not None
        ):
            await deliver.post_to_channel(
                client,
                guild_settings.results_channel_id,
                embed,
                fallback_user_id=user_id,
            )
        else:
            await deliver.dm_user(client, user_id, embed)
        # Mark AFTER the send returns. A crash here means retry re-delivers
        # — duplicate DM beats no DM.
        await idempotency.mark_delivered(redis, task_id, ttl_seconds)

    await progress.set_progress(
        state=TaskState.SUCCESS,
        meta=ProgressMeta(phase="done"),
    )
    # Drop the user→jobs index entry. Only on the success path — Taskiq
    # retries with the same task_id, so forgetting on intermediate
    # failures would make /status return "no active jobs" while a retry
    # is still running. Best-effort: a Redis blip here doesn't undo the
    # successful delivery.
    try:
        await jobs.forget_job(redis, task_id, user_id)
    except Exception as exc:
        logger.warning("forget_job failed for task %s: %s", task_id, exc)
    return {
        "url": archive_url,
        "delivery_mode": effective_delivery_mode,
        "status": "empty" if is_empty else "delivered",
    }
