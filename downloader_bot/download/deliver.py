"""Deliver the archive URL to the user (DM or guild channel)."""

import logging

import discord

logger = logging.getLogger(__name__)


class DMUnavailable(RuntimeError):
    """Raised when the user has DMs disabled or has blocked the bot.

    The orchestrator decides the fallback (e.g. post a message back in
    the originating channel asking the user to enable DMs).
    """


async def dm_user(
    client: discord.Client,
    user_id: int,
    archive_url: str,
) -> None:
    user = await client.fetch_user(user_id)
    try:
        await user.send(f"Your channel archive is ready: {archive_url}")
    except discord.Forbidden as exc:
        raise DMUnavailable(f"cannot DM user {user_id}") from exc


async def post_to_channel(
    client: discord.Client,
    channel_id: int,
    archive_url: str,
    *,
    fallback_user_id: int,
) -> None:
    """Post the archive URL in the guild's configured results channel.

    Falls back to DM'ing the requester if the channel is missing, the
    bot lacks send permission, or it's not a Messageable. The fallback
    preserves user-visibility: even if the guild's preferred channel
    breaks, the requester still gets their archive.
    """
    try:
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_id} is not messageable")
        await channel.send(
            f"<@{fallback_user_id}> your channel archive is ready: {archive_url}"
        )
    except (discord.NotFound, discord.Forbidden, TypeError) as exc:
        logger.warning(
            "results channel %s unusable (%s); falling back to DM",
            channel_id,
            exc,
        )
        await dm_user(client, fallback_user_id, archive_url)
