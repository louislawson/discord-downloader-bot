"""Result delivery for completed download jobs.

Decides whether the prepared embed (and optional fallback attachment) is sent
to the requester via DM, posted to a configured guild channel, or both, then
performs the send. Includes a Redis-backed idempotency claim so an ARQ retry
of an already-delivered job does not double-send.
"""

import logging
from dataclasses import dataclass

import asyncpg
import discord

from downloader_bot.db import guild_settings

logger = logging.getLogger("downloader_bot.worker.delivery")

# How long the per-job "delivered" marker lives in Redis. Long enough to cover
# the worst-case ARQ retry window and then some — short enough that keys don't
# accumulate forever.
_DELIVERED_KEY_TTL_SECONDS = 86_400


@dataclass
class DeliveryPayload:
    """What to send."""

    embed: discord.Embed


async def _is_delivered(redis_pool, job_id: str) -> bool:
    """Return ``True`` if ``job_id`` has already been marked delivered."""
    return await redis_pool.get(f"delivered:{job_id}") is not None


async def _mark_delivered(redis_pool, job_id: str) -> None:
    """Persist the ``delivered:{job_id}`` marker after a successful send.

    Uses ``SET NX`` so a race between concurrent runs (which ARQ shouldn't
    permit, but defence in depth) doesn't reset the TTL.
    """
    await redis_pool.set(
        f"delivered:{job_id}", "1", ex=_DELIVERED_KEY_TTL_SECONDS, nx=True
    )


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
          requester. If no channel is configured *or* the channel post
          fails (Forbidden / NotFound / 5xx), fall back to DM
          (fail-closed — no public posting without a channel).
        - ``both``    → DM first; on Forbidden, fall back to the channel.
          If no channel is configured and DM was blocked, drop with a
          warning. If the channel post itself also fails, drop with a
          warning — both routes have been attempted.

    Idempotent: a Redis-backed marker is written after the send returns
    cleanly, so ARQ retries (or the outer wrapper's failure-embed fallback)
    can re-attempt only when the original send actually raised. Forbidden
    DMs / channel-post failures count as a completed attempt and are
    marked delivered — a retry won't change a permission denial.

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
    if await _is_delivered(redis_pool, job_id):
        logger.info("Delivery for job %s already completed — skipping", job_id)
        return

    await _route(
        discord_client,
        db_pool,
        job_id,
        requester_id,
        guild_id,
        only_me,
        payload,
    )

    # Reached only when the decision tree returned without raising. A
    # transient send failure propagates out before this point, leaving the
    # marker unset so the wrapper / a retry can attempt again.
    await _mark_delivered(redis_pool, job_id)


async def _route(
    discord_client: discord.Client,
    db_pool: asyncpg.Pool,
    job_id: str,
    requester_id: int,
    guild_id: int | None,
    only_me: bool,
    payload: DeliveryPayload,
) -> None:
    """Run the DM/channel decision tree (no idempotency bookkeeping)."""
    if only_me:
        await _try_dm(
            discord_client,
            requester_id,
            payload,
            fail_closed_reason="only_me=True",
        )
        return

    mode, channel_id = await guild_settings.get(db_pool, guild_id)

    if mode == "dm":
        await _try_dm(
            discord_client,
            requester_id,
            payload,
            fail_closed_reason="mode=dm",
        )
        return

    if mode == "channel":
        if channel_id is None:
            logger.warning(
                "Job %s: mode=channel but no results channel configured for "
                "guild %s — falling back to DM",
                job_id,
                guild_id,
            )
            await _try_dm(
                discord_client,
                requester_id,
                payload,
                fail_closed_reason="mode=channel, no channel configured",
            )
            return
        if await _try_post_to_channel(
            discord_client, channel_id, requester_id, payload, job_id
        ):
            return
        # Channel post failed — fall back to DM, fail-closed (no other route).
        await _try_dm(
            discord_client,
            requester_id,
            payload,
            fail_closed_reason="mode=channel, channel post failed",
        )
        return

    # mode == "both"
    if await _try_dm(
        discord_client,
        requester_id,
        payload,
        fail_closed_reason=None,
    ):
        return
    if channel_id is None:
        logger.warning(
            "Job %s: DM blocked, mode=both, but no fallback channel configured "
            "for guild %s — dropping delivery",
            job_id,
            guild_id,
        )
        return
    if not await _try_post_to_channel(
        discord_client, channel_id, requester_id, payload, job_id
    ):
        logger.warning(
            "Job %s: DM blocked and channel post to %s failed — dropping delivery",
            job_id,
            channel_id,
        )


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
        await user.send(embed=payload.embed)
        return True
    except discord.Forbidden:
        if fail_closed_reason is not None:
            logger.warning(
                "DM to user %s blocked (%s) — failing closed",
                user_id,
                fail_closed_reason,
            )
        else:
            logger.info(
                "DM to user %s blocked — falling back to channel",
                user_id,
            )
        return False


async def _try_post_to_channel(
    discord_client: discord.Client,
    channel_id: int,
    requester_id: int,
    payload: DeliveryPayload,
    job_id: str,
) -> bool:
    """
    Post the result in a configured channel, mentioning the requester.

    Returns ``True`` on success, ``False`` if the channel can't be reached
    (deleted, forbidden) or Discord returned an error. Failures are logged
    but do not raise — the caller decides whether to fall back to DM or
    drop the delivery.
    """
    try:
        channel = await discord_client.fetch_channel(channel_id)
        await channel.send(
            content=f"<@{requester_id}>",
            embed=payload.embed,
        )
        return True
    except discord.NotFound:
        logger.warning(
            "Job %s: configured results channel %s not found", job_id, channel_id
        )
        return False
    except discord.Forbidden:
        logger.warning(
            "Job %s: forbidden to post in results channel %s",
            job_id,
            channel_id,
        )
        return False
    except discord.HTTPException:
        logger.exception(
            "Job %s: discord error posting to results channel %s",
            job_id,
            channel_id,
        )
        return False
