"""Download commands cog.

The cog does no heavy lifting — it validates the request, enqueues a
``download_channel_media`` Taskiq task, and acknowledges the user
immediately. The worker (see ``app/tasks/download.py``) runs the channel
history walk, zips, and delivery out-of-band, which avoids the 15-minute
Discord interaction-token window and lets independent channels run in
parallel.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

import discord
from discord.ext import commands
from discord.ext.commands import Context
from taskiq import AsyncTaskiqTask

from downloader_bot.config import settings
from downloader_bot.download import ratelimit
from downloader_bot.download.filters import (
    DownloadFilters,
    FilterParseError,
    parse_duration,
)
from downloader_bot.embeds import error, job_enqueued
from downloader_bot.tasks.download import DownloadResult, download_channel_media

MediaType = Literal["image", "video", "audio", "gif", "other"]
NamedPeriod = Literal[
    "today",
    "yesterday",
    "this-week",
    "last-week",
    "this-month",
    "last-month",
    "this-year",
    "last-year",
]


def _build_filters(
    *,
    media_type: MediaType | None,
    from_user: discord.User | discord.Member | None,
    before: str | None,
    after: str | None,
    during: NamedPeriod | None,
) -> DownloadFilters | None:
    """Assemble the ``DownloadFilters`` payload from cog inputs.

    Validation (``during`` conflict, duration grammar) happens before this
    call; here we only assemble the dict. ``None`` is returned when every
    input is unset so the task takes its "no filters" fast path.
    """
    payload: DownloadFilters = {}
    if media_type is not None:
        payload["category"] = media_type
    if from_user is not None:
        payload["from_user_id"] = from_user.id
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if during is not None:
        payload["during"] = during
    return payload or None


class Download(commands.Cog, name="download"):
    """Download commands cog."""

    def __init__(self, bot) -> None:
        """Bind the cog to its parent bot."""
        self.bot = bot

    @commands.hybrid_command(
        name="download",
        description="Download all media in a channel.",
    )
    async def download(
        self,
        context: Context,
        dm_me: bool = False,
        media_type: MediaType | None = None,
        from_user: discord.User | None = None,
        before: str | None = None,
        after: str | None = None,
        during: NamedPeriod | None = None,
    ) -> None:
        """Queue a background job that downloads matching media in this channel.

        ``media_type`` is intersected with the guild's ``allowed_media_types``
        policy (server admins set a ceiling; users can only narrow it).
        ``during`` is mutually exclusive with ``before`` / ``after`` —
        passing both gets a red "Conflicting filters" embed.

        Args:
            context: The command context.
            dm_me: Send the link to your DMs only and keep this command
                hidden from the channel.
            media_type: Only include attachments of this type.
            from_user: Only include attachments posted by this user.
            before: Only include messages older than this (e.g. 7d, 3w, 2h, 30m).
            after: Only include messages newer than this (e.g. 7d, 3w, 2h, 30m).
            during: Use a named time window like today, last-week, or this-month.
        """
        await context.defer(ephemeral=dm_me)

        # Cog-level permission pre-check — fails fast with a precise error
        # rather than waiting for the worker's discord.Forbidden.
        me = context.guild.me if context.guild else None
        if me and not context.channel.permissions_for(me).read_message_history:
            await context.send(
                embed=error(
                    title="Missing permission",
                    description="I need **Read Message History** in this channel.",
                ),
                ephemeral=dm_me,
            )
            return

        # Filter validation. `during` is mutually exclusive with `before` /
        # `after` (a single window vs an arbitrary one); rejecting at the
        # cog gives the user a typed error rather than a worker traceback.
        if during is not None and (before is not None or after is not None):
            await context.send(
                embed=error(
                    title="Conflicting filters",
                    description=(
                        "`during` can't be combined with `before` or `after` — "
                        "pick one way to express the time window."
                    ),
                ),
                ephemeral=dm_me,
            )
            return

        try:
            if before is not None:
                parse_duration(before)
            if after is not None:
                parse_duration(after)
        except FilterParseError as exc:
            await context.send(
                embed=error(title="Invalid filter", description=str(exc)),
                ephemeral=dm_me,
            )
            return

        # Per-guild rate limit. Owner bypass; DM-context /download has no
        # guild to rate-limit, so the check is skipped. Reply is always
        # ephemeral so a rate-limit hit doesn't itself spam the channel.
        if context.guild and context.author.id != context.guild.owner_id:
            allowed, retry_after = await ratelimit.acquire(
                self.bot.redis,
                context.guild.id,
                capacity=settings.GUILD_RATE_LIMIT_BURST,
                refill_per_hour=settings.GUILD_RATE_LIMIT_PER_HOUR,
            )
            if not allowed:
                retry_at = datetime.now(UTC) + timedelta(seconds=retry_after)
                await context.send(
                    embed=error(
                        title="Rate limit reached",
                        description=(
                            "This server has used its `/download` allowance. "
                            f"Try again {discord.utils.format_dt(retry_at, 'R')}."
                        ),
                    ),
                    ephemeral=True,
                )
                return

        filters_payload = _build_filters(
            media_type=media_type,
            from_user=from_user,
            before=before,
            after=after,
            during=during,
        )

        try:
            task: AsyncTaskiqTask[DownloadResult] = await download_channel_media.kiq(
                channel_id=context.channel.id,
                user_id=context.author.id,
                guild_id=context.guild.id if context.guild else None,
                dm_me=dm_me,
                filters=filters_payload,
            )
        except Exception as exc:
            # Broker was alive at bot startup but became unreachable
            # mid-flight (RabbitMQ down, network blip, auth rotated).
            self.bot.logger.exception(
                "Failed to enqueue download for channel %s: %s",
                context.channel.id,
                exc,
            )
            await context.send(
                embed=error(
                    title="Service unavailable",
                    description=(
                        "The download queue is not currently available. "
                        "Please try again in a moment."
                    ),
                ),
                ephemeral=dm_me,
            )
            return

        self.bot.logger.info(
            "Enqueued download task %s for channel %s "
            "(requester=%s, guild=%s, dm_me=%s, filters=%s)",
            task.task_id,
            context.channel.id,
            context.author.id,
            context.guild.id if context.guild else None,
            dm_me,
            filters_payload,
        )

        await context.send(
            embed=job_enqueued(task_id=task.task_id, dm_me=dm_me),
            ephemeral=dm_me,
        )


async def setup(bot) -> None:
    """Load this cog into a bot."""
    await bot.add_cog(Download(bot))
