"""Download commands cog.

The cog does no heavy lifting — it validates the request, enqueues a
``download_channel_media`` Taskiq task, and acknowledges the user
immediately. The worker (see ``app/tasks/download.py``) runs the channel
history walk, zips, and delivery out-of-band, which avoids the 15-minute
Discord interaction-token window and lets independent channels run in
parallel.
"""

import discord
from discord.ext import commands
from discord.ext.commands import Context
from taskiq import AsyncTaskiqTask

from downloader_bot.embeds import error, info
from downloader_bot.tasks.download import DownloadResult, download_channel_media


def _queued_embed(task_id: str, only_me: bool) -> discord.Embed:
    """Blurple ack shown immediately after enqueueing the task."""
    embed = info(
        title="Download queued",
        description=(
            "Your download has been queued. The result will be sent to "
            "you via DM once it's ready."
            if only_me
            else "Your download has been queued. You'll be notified once it's ready."
        ),
    )
    embed.set_footer(text=f"Job {task_id}")
    return embed


class Download(commands.Cog, name="download"):
    """Download commands cog."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="download",
        description="Download all media in a channel.",
    )
    async def download(self, context: Context, only_me: bool = False) -> None:
        """Queue a background job that downloads all media in the current channel.

        Args:
            context: The command context.
            only_me: Force DM delivery regardless of guild setting, and
                hide the queued-ack from other channel members.
        """
        await context.defer(ephemeral=only_me)

        # Cog-level permission pre-check — fails fast with a precise error
        # rather than waiting for the worker's discord.Forbidden.
        me = context.guild.me if context.guild else None
        if me and not context.channel.permissions_for(me).read_message_history:
            await context.send(
                embed=error(
                    title="Missing permission",
                    description="I need **Read Message History** in this channel.",
                ),
                ephemeral=only_me,
            )
            return

        try:
            task: AsyncTaskiqTask[DownloadResult] = await download_channel_media.kiq(
                channel_id=context.channel.id,
                user_id=context.author.id,
                guild_id=context.guild.id if context.guild else None,
                only_me=only_me,
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
                ephemeral=only_me,
            )
            return

        self.bot.logger.info(
            "Enqueued download task %s for channel %s "
            "(requester=%s, guild=%s, only_me=%s)",
            task.task_id,
            context.channel.id,
            context.author.id,
            context.guild.id if context.guild else None,
            only_me,
        )

        await context.send(
            embed=_queued_embed(task.task_id, only_me),
            ephemeral=only_me,
        )


async def setup(bot) -> None:
    """Load this cog into a bot."""
    await bot.add_cog(Download(bot))
