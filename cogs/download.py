"""Download commands cog.

The cog itself does no heavy lifting — it validates the request, enqueues a
``download_channel_media`` ARQ job, and acknowledges the user immediately.
The worker (see ``worker/jobs.py``) runs the channel history walk, zips, and
delivery out-of-band, which avoids the 15-minute Discord interaction-token
window and lets independent channels run in parallel.
"""

import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context

from config import settings


def _queued_embed(job_id: str, only_me: bool) -> discord.Embed:
    """Blurple ack shown immediately after enqueueing the job."""
    description = (
        "Your download has been queued. The result will be sent to you "
        "via DM once it's ready."
        if only_me
        else "Your download has been queued. You'll be notified once it's ready."
    )
    embed = discord.Embed(
        title="Download queued",
        description=description,
        colour=discord.Color.blurple(),
        timestamp=datetime.now(),
    )
    embed.set_footer(text=f"Job {job_id}")
    return embed


def _error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        colour=discord.Color.red(),
        timestamp=datetime.now(),
    )


class Download(commands.Cog, name="download"):
    """
    Download commands cog.

    Attributes:
        bot (DiscordBot): DiscordBot instance.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="download",
        description="Download all media in a channel.",
    )
    async def download(self, context: Context, only_me: bool = False) -> None:
        """
        Queue a background job that downloads all media in the current channel.

        Args:
            context (Context): The command context.
            only_me (bool): Only deliver the result privately via DM.
        """
        await context.defer(ephemeral=only_me)

        if self.bot.arq_pool is None:
            self.bot.logger.error(
                "Download requested but ARQ pool is not initialised"
            )
            await context.send(
                embed=_error_embed(
                    "Service unavailable",
                    "The download queue is not currently available. "
                    "Please try again in a moment.",
                ),
                ephemeral=only_me,
            )
            return

        job_id = uuid.uuid4().hex
        payload = {
            "job_id": job_id,
            "channel_id": context.channel.id,
            "guild_id": context.guild.id if context.guild is not None else None,
            "requester_id": context.author.id,
            "requester_tag": str(context.author),
            "only_me": only_me,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "allowed_media_types": list(settings.ALLOWED_MEDIA_TYPES),
        }

        await self.bot.arq_pool.enqueue_job(
            "download_channel_media", payload, _job_id=job_id,
        )
        self.bot.logger.info(
            "Enqueued download job %s for channel %s (requester=%s, only_me=%s)",
            job_id, context.channel.id, context.author.id, only_me,
        )

        await context.send(
            embed=_queued_embed(job_id, only_me), ephemeral=only_me,
        )


async def setup(bot) -> None:
    """
    Used to load this cog into a Bot.

    Args:
        bot (DiscordBot): The bot instance to load this cog.
    """
    await bot.add_cog(Download(bot))
