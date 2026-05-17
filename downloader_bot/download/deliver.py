"""Deliver an embed to the user (DM or guild channel).

The orchestrator builds the embed (success URL or empty-channel error)
and hands it to these helpers; routing is the orchestrator's call, the
send mechanics live here.
"""

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
    embed: discord.Embed,
) -> None:
    """DM the requester with the given embed.

    Args:
        client: REST-only discord.py client (worker-shared).
        user_id: Discord user ID to DM.
        embed: Pre-built embed to send (success URL or error notice).

    Raises:
        DMUnavailable: The user has DMs disabled or has blocked the bot
            (``discord.Forbidden`` is wrapped to keep the storage / delivery
            error surface backend-agnostic).
    """
    user = await client.fetch_user(user_id)
    try:
        await user.send(embed=embed)
    except discord.Forbidden as exc:
        raise DMUnavailable(f"cannot DM user {user_id}") from exc


async def post_to_channel(
    client: discord.Client,
    channel_id: int,
    embed: discord.Embed,
    *,
    fallback_user_id: int,
) -> None:
    """Post the embed in the guild's configured results channel.

    Falls back to DM'ing the requester (with the same embed) if the
    channel is missing, the bot lacks send permission, or it's not a
    Messageable. The fallback preserves user-visibility: even if the
    guild's preferred channel breaks, the requester still gets notified.
    """
    try:
        channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_id} is not messageable")
        await channel.send(embed=embed)
    except (discord.NotFound, discord.Forbidden, TypeError) as exc:
        logger.warning(
            "results channel %s unusable (%s); falling back to DM",
            channel_id,
            exc,
        )
        await dm_user(client, fallback_user_id, embed)
