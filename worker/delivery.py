"""Result delivery for completed download jobs.

Decides whether the prepared embed (and optional fallback attachment) is sent
to the requester via DM, posted to a configured guild channel, or both, then
performs the send. Includes a Redis-backed idempotency claim so an ARQ retry
of an already-delivered job does not double-send.
"""

import logging
from dataclasses import dataclass
from io import BytesIO

import asyncpg
import discord

from db import guild_settings


logger = logging.getLogger("downloader_bot.worker.delivery")

# How long the per-job "delivered" marker lives in Redis. Long enough to cover
# the worst-case ARQ retry window and then some — short enough that keys don't
# accumulate forever.
_DELIVERED_KEY_TTL_SECONDS = 86_400


@dataclass
class DeliveryPayload:
    """What to send. ``attachment`` is ``(buffer, filename)`` or ``None``."""

    embed: discord.Embed
    attachment: tuple[BytesIO, str] | None = None


def _make_file(attachment: tuple[BytesIO, str] | None) -> discord.File | None:
    """
    Build a fresh ``discord.File`` from the buffer, rewinding it first.

    ``discord.File`` consumes its underlying ``fp`` during ``send``, so a new
    File must be created for every attempt — important when DM fails and we
    fall back to a channel post.
    """
    if attachment is None:
        return None
    buffer, filename = attachment
    buffer.seek(0)
    return discord.File(buffer, filename=filename)


async def _claim_delivery(redis_pool, job_id: str) -> bool:
    """
    Atomically claim the delivery slot for ``job_id``.

    Returns ``True`` exactly once per job_id within the TTL window. Subsequent
    callers (e.g. ARQ retrying a job whose first run did deliver) get
    ``False`` and must skip — that's the idempotency guarantee.
    """
    claimed = await redis_pool.set(
        f"delivered:{job_id}", "1", ex=_DELIVERED_KEY_TTL_SECONDS, nx=True
    )
    return bool(claimed)


async def deliver(
    discord_client: discord.Client,
    redis_pool,
    db_pool: asyncpg.Pool,
    job_id: str,
    requester_id: int,
    guild_id: int | None,
    only_me: bool,
    payload: DeliveryPayload,
) -> None:
    """
    Send ``payload`` to the right destination per the delivery rules.

    Decision tree:

    - ``only_me=True`` → DM the requester. Forbidden → fail closed (never
      post a public link if the requester explicitly asked for privacy).
    - Otherwise the guild's mode decides:
        - ``dm``      → DM the requester; Forbidden → fail closed.
        - ``channel`` → post in the configured channel mentioning the
          requester. If no channel is configured, fall back to DM
          (fail-closed on Forbidden — no public posting without a channel).
        - ``both``    → DM first; on Forbidden, fall back to the channel.
          If no channel is configured and DM was blocked, drop with a
          warning (DM was already attempted; no other route remains).

    Idempotent: a Redis-backed claim ensures ARQ retries don't double-deliver.

    Args:
        discord_client (discord.Client): REST-only client.
        redis_pool: ARQ Redis connection (``ctx['redis']``).
        db_pool (asyncpg.Pool): Postgres pool used to read guild settings.
        job_id (str): Unique job id, used as the dedup key.
        requester_id (int): Discord user id who invoked the command.
        guild_id (int | None): Guild id (None for DM-context invocations).
        only_me (bool): Whether the result must stay private.
        payload (DeliveryPayload): The embed and optional attachment.
    """
    if not await _claim_delivery(redis_pool, job_id):
        logger.info("Delivery for job %s already claimed — skipping", job_id)
        return

    if only_me:
        await _try_dm(
            discord_client, requester_id, payload,
            fail_closed_reason="only_me=True",
        )
        return

    mode, channel_id = await guild_settings.get(db_pool, guild_id)

    if mode == "dm":
        await _try_dm(
            discord_client, requester_id, payload, fail_closed_reason="mode=dm",
        )
        return

    if mode == "channel":
        if channel_id is None:
            logger.warning(
                "Job %s: mode=channel but no results channel configured for "
                "guild %s — falling back to DM",
                job_id, guild_id,
            )
            await _try_dm(
                discord_client, requester_id, payload,
                fail_closed_reason="mode=channel, no channel configured",
            )
            return
        await _post_to_channel(discord_client, channel_id, requester_id, payload)
        return

    # mode == "both"
    if await _try_dm(
        discord_client, requester_id, payload, fail_closed_reason=None,
    ):
        return
    if channel_id is None:
        logger.warning(
            "Job %s: DM blocked, mode=both, but no fallback channel configured "
            "for guild %s — dropping delivery",
            job_id, guild_id,
        )
        return
    await _post_to_channel(discord_client, channel_id, requester_id, payload)


async def _try_dm(
    discord_client: discord.Client,
    user_id: int,
    payload: DeliveryPayload,
    *,
    fail_closed_reason: str | None,
) -> bool:
    """
    Try to DM the user. Returns ``True`` on success, ``False`` on Forbidden.

    If ``fail_closed_reason`` is set, Forbidden is logged and the function
    returns ``False`` — the caller should NOT fall back to a public channel.
    If ``None``, Forbidden returns ``False`` quietly so the caller can fall
    back.
    """
    try:
        user = await discord_client.fetch_user(user_id)
        await user.send(embed=payload.embed, file=_make_file(payload.attachment))
        return True
    except discord.Forbidden:
        if fail_closed_reason is not None:
            logger.warning(
                "DM to user %s blocked (%s) — failing closed",
                user_id, fail_closed_reason,
            )
        else:
            logger.info(
                "DM to user %s blocked — falling back to channel", user_id,
            )
        return False


async def _post_to_channel(
    discord_client: discord.Client,
    channel_id: int,
    requester_id: int,
    payload: DeliveryPayload,
) -> None:
    """Post the result in a configured channel, mentioning the requester."""
    channel = await discord_client.fetch_channel(channel_id)
    await channel.send(
        content=f"<@{requester_id}>",
        embed=payload.embed,
        file=_make_file(payload.attachment),
    )
