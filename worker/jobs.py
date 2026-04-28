"""Background jobs for the downloader bot worker.

Jobs receive an ARQ ``ctx`` dict (with ``job_id``, ``redis``, plus whatever
``on_startup`` populated — namely ``discord_client``) and a job-specific
payload dict.
"""

import logging
from datetime import datetime
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import discord

from config import settings
from storage.container import ContainerRepository
from storage.exceptions import (
    BlobUploadError,
    ContainerConfigError,
    SasGenerationError,
)
from worker.delivery import DeliveryPayload, deliver


logger = logging.getLogger("downloader_bot.worker.jobs")


def _guild_upload_limit(guild: discord.Guild | None) -> int:
    """
    Return the file upload limit in bytes for ``guild``.

    Same boost-tier table the bot used to consult; duplicated here so the
    worker is self-contained.
    """
    if guild is None:
        return 8 * 1024 * 1024  # DMs / unknown guild
    tier = guild.premium_tier
    if tier >= 3:
        return 100 * 1024 * 1024
    if tier == 2:
        return 50 * 1024 * 1024
    return 8 * 1024 * 1024


async def _resolve_guild(
    client: discord.Client, guild_id: int | None,
) -> discord.Guild | None:
    """
    Fetch a real ``Guild`` over REST so ``premium_tier`` is populated.

    ``client.fetch_channel(...)`` in REST-only mode returns a channel whose
    ``.guild`` is a placeholder ``Object`` (no tier info). We need a real
    Guild to compute the upload-fallback limit.
    """
    if guild_id is None:
        return None
    try:
        return await client.fetch_guild(guild_id)
    except discord.HTTPException:
        logger.warning(
            "Could not fetch guild %s — defaulting to tier 0 upload limit",
            guild_id,
        )
        return None


def _success_embed(
    sas_url: str, image_count: int, video_count: int, requester_tag: str,
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


def _attachment_fallback_embed(
    image_count: int, video_count: int, requester_tag: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Channel Media Download (direct attachment)",
        description=(
            "Azure storage was unavailable, so the archive is attached "
            "directly. Note: this file will expire when Discord removes "
            "the attachment."
        ),
        colour=discord.Color.orange(),
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


async def download_channel_media(ctx: dict, payload: dict) -> dict:
    """
    Bundle a channel's allowed-MIME attachments into a zip, upload to Azure,
    and deliver the SAS link (or fallback attachment) via DM/channel.

    The four phases match the original cog: collect → validate → upload →
    deliver. ``deliver`` handles DM/channel routing and idempotency.

    Args:
        ctx (dict): ARQ context. Reads ``discord_client`` and ``db_pool``
            (both added by ``on_startup``), ``redis``, and ``job_id``.
        payload (dict): Job payload (see ``cogs/download.py`` for shape).

    Returns:
        dict: ``{"ok": bool, ...}`` summary stored by ARQ.
    """
    discord_client: discord.Client = ctx["discord_client"]
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
        job_id, channel_id, guild_id, requester_id, only_me,
    )

    channel = await discord_client.fetch_channel(channel_id)

    image_count = 0
    video_count = 0

    # --- Phase A: collect and zip -------------------------------------------
    zip_buffer = BytesIO()
    try:
        with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zip_file:
            async for message in channel.history(limit=None):
                for attachment in message.attachments:
                    if attachment.content_type not in allowed_types:
                        continue

                    if "image" in attachment.content_type:
                        image_count += 1
                    if "video" in attachment.content_type:
                        video_count += 1

                    att_buffer = BytesIO()
                    try:
                        await attachment.save(att_buffer)
                    except discord.HTTPException as e:
                        logger.warning(
                            "Skipped attachment '%s' (msg %s): %s",
                            attachment.filename, message.id, e,
                        )
                        att_buffer.close()
                        continue

                    zip_file.writestr(
                        f"{message.id}_{attachment.filename}",
                        att_buffer.getvalue(),
                    )
                    att_buffer.close()
    except discord.Forbidden:
        logger.warning(
            "Job %s: missing permission to read history of channel %s",
            job_id, channel_id,
        )
        await deliver(
            discord_client, redis_pool, db_pool, job_id,
            requester_id, guild_id, only_me,
            DeliveryPayload(embed=_error_embed(
                "Missing permissions",
                "I don't have permission to read the history of that channel.",
            )),
        )
        zip_buffer.close()
        return {"ok": False, "reason": "forbidden"}
    except discord.HTTPException as e:
        logger.exception(
            "Job %s: discord error during history walk: %s", job_id, e,
        )
        await deliver(
            discord_client, redis_pool, db_pool, job_id,
            requester_id, guild_id, only_me,
            DeliveryPayload(embed=_error_embed(
                "Discord error",
                "An unexpected Discord error occurred while reading the "
                "channel's history. Please try again later.",
            )),
        )
        zip_buffer.close()
        return {"ok": False, "reason": "discord_http"}

    # --- Phase B: nothing to zip --------------------------------------------
    if image_count == 0 and video_count == 0:
        logger.info(
            "Job %s: no allowed media found in channel %s", job_id, channel_id,
        )
        await deliver(
            discord_client, redis_pool, db_pool, job_id,
            requester_id, guild_id, only_me,
            DeliveryPayload(embed=_error_embed(
                "No media found",
                "No allowed media types were found in that channel.",
            )),
        )
        zip_buffer.close()
        return {"ok": False, "reason": "empty"}

    zip_buffer.seek(0)
    zip_filename = f"{getattr(channel, 'name', 'channel')}-media.zip"
    zip_size = zip_buffer.getbuffer().nbytes

    # --- Phase C: upload to Azure -------------------------------------------
    guild = await _resolve_guild(discord_client, guild_id)
    upload_limit = _guild_upload_limit(guild)

    try:
        async with ContainerRepository() as container:
            blob_client = await container.create(
                name=zip_filename, data=zip_buffer, overwrite=True,
            )
            sas_url = await container.sas_url(
                blob_name=blob_client.blob_name, blob_url=blob_client.url,
            )
            if settings.ENVIRONMENT == "dev":
                sas_url = sas_url.replace(settings.ST_INT_URL, settings.ST_EXT_URL)

    except ContainerConfigError:
        logger.exception("Job %s: storage misconfigured", job_id)
        await deliver(
            discord_client, redis_pool, db_pool, job_id,
            requester_id, guild_id, only_me,
            DeliveryPayload(embed=_error_embed(
                "Storage misconfigured",
                "The bot's storage backend is not configured correctly. "
                "Please contact an administrator.",
            )),
        )
        zip_buffer.close()
        return {"ok": False, "reason": "config"}

    except (BlobUploadError, SasGenerationError) as e:
        logger.exception(
            "Job %s: Azure storage failed (%s) — attempting attachment fallback",
            job_id, type(e).__name__,
        )
        if zip_size <= upload_limit:
            try:
                await deliver(
                    discord_client, redis_pool, job_id,
                    requester_id, guild_id, only_me,
                    DeliveryPayload(
                        embed=_attachment_fallback_embed(
                            image_count, video_count, requester_tag,
                        ),
                        attachment=(zip_buffer, zip_filename),
                    ),
                )
                return {"ok": True, "fallback": "attachment"}
            finally:
                zip_buffer.close()
        else:
            await deliver(
                discord_client, redis_pool, job_id,
                requester_id, guild_id, only_me,
                DeliveryPayload(embed=_error_embed(
                    "Upload failed",
                    "The media archive could not be uploaded to storage, and "
                    f"it is too large ({zip_size // (1024 * 1024)} MB) to "
                    "send as a Discord attachment. Please try again later or "
                    "contact an administrator.",
                )),
            )
            zip_buffer.close()
            return {"ok": False, "reason": "upload_failed_too_large"}

    # --- Phase D: deliver the SAS link --------------------------------------
    try:
        await deliver(
            discord_client, redis_pool, db_pool, job_id,
            requester_id, guild_id, only_me,
            DeliveryPayload(embed=_success_embed(
                sas_url, image_count, video_count, requester_tag,
            )),
        )
        return {"ok": True, "sas_url": sas_url}
    finally:
        if not zip_buffer.closed:
            zip_buffer.close()
