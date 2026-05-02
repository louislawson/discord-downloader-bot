"""Background jobs for the downloader bot worker.

Jobs receive an ARQ ``ctx`` dict (with ``job_id``, ``redis``, plus whatever
``on_startup`` populated — namely ``discord_client``, ``db_pool``, and
``http``) and a job-specific payload dict.
"""

import logging
from datetime import datetime

import aiohttp
import discord
from arq.worker import Retry, RetryJob

from downloader_bot.config import settings
from downloader_bot.storage import get_storage_backend
from downloader_bot.storage.exceptions import (
    SignedUrlError,
    StorageConfigError,
    UploadError,
)
from downloader_bot.worker.delivery import DeliveryPayload, deliver
from downloader_bot.worker.zip_stream import (
    AttachmentStreamError,
    build_zip_stream,
)

logger = logging.getLogger("downloader_bot.worker.jobs")


def _success_embed(
    sas_url: str,
    image_count: int,
    video_count: int,
    requester_tag: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Channel Media Download",
        description=f"[Download channel media]({sas_url})",
        colour=discord.Color.green(),
        timestamp=datetime.now(),
    )
    embed.set_author(name="Downloader Bot")
    embed.add_field(name="Images", value=str(image_count), inline=True)
    embed.add_field(name="Videos", value=str(video_count), inline=True)
    embed.set_footer(text=f"Requested by {requester_tag}")
    return embed


def _error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        colour=discord.Color.red(),
        timestamp=datetime.now(),
    )


def _unhandled_failure_embed() -> discord.Embed:
    """Generic last-resort embed used when the job dies on an unhandled error."""
    return discord.Embed(
        title="Download failed",
        description=(
            "The download job failed unexpectedly. Please try again later, "
            "or contact an administrator if this keeps happening."
        ),
        colour=discord.Color.red(),
        timestamp=datetime.now(),
    )


async def download_channel_media(ctx: dict, payload: dict) -> dict:
    """
    Public ARQ entrypoint. Delegates to ``_run_download_channel_media`` and,
    on any unhandled-exception path, delivers a generic failure embed
    before re-raising so the requester isn't left waiting on a job that
    will never deliver.

    ARQ's ``max_tries`` only governs ``Retry``/``RetryJob``-driven retries —
    arbitrary exceptions go straight to a permanent ``! ... failed``, so by
    the time we land in the ``except Exception`` branch the job is over and
    we always need to surface something to the user. ``Retry``/``RetryJob``
    are re-raised untouched so ARQ's retry signaling still works if a
    future code path uses it.

    Anticipated errors (Forbidden history walks, missing storage config,
    upload failures, mid-stream attachment failures, etc.) are handled
    inside the body and produce their own targeted embeds — this wrapper
    only fires for everything else (network blips, transient Discord 5xx,
    unexpected SDK errors).
    """
    job_id: str = ctx["job_id"]
    try:
        return await _run_download_channel_media(ctx, payload)
    except (Retry, RetryJob):
        raise
    except Exception:
        logger.exception(
            "Job %s: unhandled exception, delivering failure embed", job_id
        )
        try:
            await deliver(
                ctx["discord_client"],
                ctx["redis"],
                ctx["db_pool"],
                job_id,
                payload["requester_id"],
                payload.get("guild_id"),
                payload.get("only_me", False),
                DeliveryPayload(embed=_unhandled_failure_embed()),
            )
        except Exception:
            logger.exception(
                "Job %s: failure-notification delivery itself failed",
                job_id,
            )
        raise


async def _run_download_channel_media(ctx: dict, payload: dict) -> dict:
    """
    Stream a channel's allowed-MIME attachments into a zip, upload the
    stream to the configured storage backend, and deliver the pre-signed
    link via DM/channel.

    Memory is bounded: the pipeline composes ``channel.history()`` →
    aiohttp chunked GET → ``stream-zip`` async generator →
    ``ContainerClient.upload_blob`` so no stage materialises the full
    archive. ``deliver`` handles DM/channel routing and idempotency.

    Args:
        ctx (dict): ARQ context. Reads ``discord_client``, ``db_pool``, and
            ``http`` (all added by ``on_startup``), plus ``redis`` and
            ``job_id``.
        payload (dict): Job payload (see ``cogs/download.py`` for shape).

    Returns:
        dict: ``{"ok": bool, ...}`` summary stored by ARQ.
    """
    discord_client: discord.Client = ctx["discord_client"]
    http_session: aiohttp.ClientSession = ctx["http"]
    redis_pool = ctx["redis"]
    db_pool = ctx["db_pool"]
    job_id: str = ctx["job_id"]

    channel_id = payload["channel_id"]
    guild_id = payload["guild_id"]
    requester_id = payload["requester_id"]
    requester_tag = payload["requester_tag"]
    only_me = payload["only_me"]
    allowed_types = set(payload["allowed_media_types"])

    logger.info(
        "Job %s started: channel=%s guild=%s requester=%s only_me=%s",
        job_id,
        channel_id,
        guild_id,
        requester_id,
        only_me,
    )

    channel = await discord_client.fetch_channel(channel_id)

    # --- Phase A: build the streaming pipeline (no I/O yet) -----------------
    stream = build_zip_stream(
        http_session, channel, allowed_types, settings.ATTACHMENT_CHUNK_SIZE
    )
    zip_filename = f"{getattr(channel, 'name', 'channel')}-media.zip"

    # --- Phases B+C+D inside a single storage context -----------------------
    # The storage context wraps upload, empty-channel cleanup, and signed-URL
    # delivery so we never reopen the backend for the orphan-blob delete.
    try:
        async with get_storage_backend() as storage:
            try:
                signed_url = await storage.upload_and_sign(
                    name=zip_filename,
                    data=stream.iterable,
                    overwrite=True,
                )
            except discord.Forbidden:
                # Raised lazily from inside channel.history() once stream-zip
                # starts pulling members.
                logger.warning(
                    "Job %s: missing permission to read history of channel %s",
                    job_id,
                    channel_id,
                )
                await deliver(
                    discord_client,
                    redis_pool,
                    db_pool,
                    job_id,
                    requester_id,
                    guild_id,
                    only_me,
                    DeliveryPayload(
                        embed=_error_embed(
                            "Missing permissions",
                            "I don't have permission to read the history "
                            "of that channel.",
                        )
                    ),
                )
                return {"ok": False, "reason": "forbidden"}
            except discord.HTTPException:
                logger.exception("Job %s: discord error during history walk", job_id)
                await deliver(
                    discord_client,
                    redis_pool,
                    db_pool,
                    job_id,
                    requester_id,
                    guild_id,
                    only_me,
                    DeliveryPayload(
                        embed=_error_embed(
                            "Discord error",
                            "An unexpected Discord error occurred while "
                            "reading the channel's history. Please try "
                            "again later.",
                        )
                    ),
                )
                return {"ok": False, "reason": "discord_http"}
            except AttachmentStreamError:
                logger.exception("Job %s: attachment stream failed mid-flight", job_id)
                await deliver(
                    discord_client,
                    redis_pool,
                    db_pool,
                    job_id,
                    requester_id,
                    guild_id,
                    only_me,
                    DeliveryPayload(
                        embed=_error_embed(
                            "Discord error",
                            "An attachment failed to download partway "
                            "through. Please try again later.",
                        )
                    ),
                )
                return {"ok": False, "reason": "attachment_stream"}
            except (UploadError, SignedUrlError):
                logger.exception("Job %s: storage backend failed", job_id)
                await deliver(
                    discord_client,
                    redis_pool,
                    db_pool,
                    job_id,
                    requester_id,
                    guild_id,
                    only_me,
                    DeliveryPayload(
                        embed=_error_embed(
                            "Upload failed",
                            "The media archive could not be uploaded to "
                            "storage. Please try again later or contact an "
                            "administrator.",
                        )
                    ),
                )
                return {"ok": False, "reason": "upload_failed"}

            # --- Empty-channel cleanup (reuses the same storage context) ---
            if stream.counters.images == 0 and stream.counters.videos == 0:
                # Best-effort delete of the orphan empty zip; swallow errors
                # and let Azure's 7-day lifecycle policy reap leftovers.
                try:
                    await storage.delete_blob(zip_filename)
                except Exception:
                    logger.warning(
                        "Job %s: best-effort delete of empty zip '%s' failed; "
                        "relying on lifecycle policy",
                        job_id,
                        zip_filename,
                    )
                logger.info(
                    "Job %s: no allowed media found in channel %s",
                    job_id,
                    channel_id,
                )
                await deliver(
                    discord_client,
                    redis_pool,
                    db_pool,
                    job_id,
                    requester_id,
                    guild_id,
                    only_me,
                    DeliveryPayload(
                        embed=_error_embed(
                            "No media found",
                            "No allowed media types were found in that channel.",
                        )
                    ),
                )
                return {"ok": False, "reason": "empty"}

            # --- Phase D: deliver SAS URL --------------------------------
            await deliver(
                discord_client,
                redis_pool,
                db_pool,
                job_id,
                requester_id,
                guild_id,
                only_me,
                DeliveryPayload(
                    embed=_success_embed(
                        signed_url,
                        stream.counters.images,
                        stream.counters.videos,
                        requester_tag,
                    )
                ),
            )
            return {"ok": True, "sas_url": signed_url}
    except StorageConfigError:
        # Raised by get_storage_backend() itself before the context opens —
        # belongs outside the inner try/except.
        logger.exception("Job %s: storage misconfigured", job_id)
        await deliver(
            discord_client,
            redis_pool,
            db_pool,
            job_id,
            requester_id,
            guild_id,
            only_me,
            DeliveryPayload(
                embed=_error_embed(
                    "Storage misconfigured",
                    "The bot's storage backend is not configured correctly. "
                    "Please contact an administrator.",
                )
            ),
        )
        return {"ok": False, "reason": "config"}
