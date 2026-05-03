"""Per-guild setup commands.

Server-owner-only. Configures how completed download jobs are delivered:

- ``mode``    — pick ``dm``, ``channel``, or ``both``
- ``channel`` — set the channel used by ``channel`` mode (and as the fallback
                target for ``both``)
- ``clear``   — unset the configured results channel
- ``show``    — print current settings
"""

from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context, errors

from downloader_bot.db import guild_settings
from downloader_bot.embeds import error, info, success


class NotGuildOwner(commands.CheckFailure):
    """Raised when a non-owner tries to run an owner-gated guild command."""


def _is_guild_owner():
    """Check that succeeds only for the guild owner. Raises in DMs too."""

    async def predicate(context: Context) -> bool:
        if context.guild is None:
            raise commands.NoPrivateMessage()
        if context.author.id != context.guild.owner_id:
            raise NotGuildOwner("Only the server owner can configure this bot.")
        return True

    return commands.check(predicate)


class Setup(commands.Cog, name="setup"):
    """
    Per-guild configuration commands.

    Attributes:
        bot (DiscordBot): The bot instance.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _ensure_db(self, context: Context) -> bool:
        """Ack-and-bail if the db pool isn't ready yet."""
        if self.bot.db_pool is None:
            self.bot.logger.error(
                "Setup command invoked but db_pool is not initialised"
            )
            await context.send(
                embed=error(
                    title="Service unavailable",
                    description="The configuration store is not currently available. Please try again in a moment.",
                ),
                ephemeral=True,
            )
            return False
        return True

    @commands.hybrid_group(
        name="setup",
        description="Configure download delivery for this server.",
    )
    @commands.guild_only()
    @_is_guild_owner()
    async def setup_group(self, context: Context) -> None:
        """Show usage when invoked without a subcommand (prefix invocation only)."""
        if context.invoked_subcommand is None:
            await context.send(
                embed=info(
                    title="Setup",
                    description=f"Use `{self.bot.bot_prefix}setup mode | channel | clear | show` (or the `/setup` slash command).",
                ),
                ephemeral=True,
            )

    @setup_group.command(
        name="mode",
        description="Set how download results are delivered.",
    )
    @app_commands.describe(
        mode="dm = private DM | channel = post in channel | both = DM with channel fallback",
    )
    async def setup_mode(
        self,
        context: Context,
        mode: Literal["dm", "channel", "both"],
    ) -> None:
        """Update the delivery mode for this guild."""
        if not await self._ensure_db(context):
            return
        await guild_settings.set_mode(self.bot.db_pool, context.guild.id, mode)
        await context.send(
            embed=success(
                title="Updated",
                description=f"Delivery mode set to `{mode}`.",
            ),
            ephemeral=True,
        )

    @setup_group.command(
        name="channel",
        description="Set the channel used for posting download results.",
    )
    @app_commands.describe(channel="The channel where results should be posted.")
    async def setup_channel(
        self,
        context: Context,
        channel: discord.TextChannel,
    ) -> None:
        """Set the results channel for this guild."""
        if not await self._ensure_db(context):
            return
        if channel.guild.id != context.guild.id:
            await context.send(
                embed=error(
                    title="Wrong server",
                    description="That channel doesn't belong to this server.",
                ),
                ephemeral=True,
            )
            return
        await guild_settings.set_channel(
            self.bot.db_pool,
            context.guild.id,
            channel.id,
        )
        await context.send(
            embed=success(
                title="Updated",
                description=f"Results channel set to {channel.mention}.",
            ),
            ephemeral=True,
        )

    @setup_group.command(
        name="clear",
        description="Unset the configured results channel.",
    )
    async def setup_clear(self, context: Context) -> None:
        """Clear the configured results channel for this guild."""
        if not await self._ensure_db(context):
            return
        await guild_settings.clear_channel(self.bot.db_pool, context.guild.id)
        await context.send(
            embed=success(
                title="Updated",
                description="Results channel cleared.",
            ),
            ephemeral=True,
        )

    @setup_group.command(
        name="show",
        description="Show current delivery settings.",
    )
    async def setup_show(self, context: Context) -> None:
        """Display current delivery settings for this guild."""
        if not await self._ensure_db(context):
            return
        mode, channel_id = await guild_settings.get(
            self.bot.db_pool,
            context.guild.id,
        )
        channel_str = f"<#{channel_id}>" if channel_id else "_not set_"
        await context.send(
            embed=info(
                title="Delivery settings",
                description=f"**Mode:** `{mode}`\n**Channel:** {channel_str}",
            ),
            ephemeral=True,
        )

    async def cog_command_error(
        self,
        context: Context,
        cmd_error: errors.CommandError,
    ) -> None:
        """
        Handle setup-specific errors before the global handler sees them.

        ``NotGuildOwner`` and ``NoPrivateMessage`` get a tailored message; all
        other errors re-raise so the global handler in [bot.py](bot.py)
        formats them.
        """
        if isinstance(cmd_error, NotGuildOwner):
            await context.send(
                embed=error(
                    title="Server owner only",
                    description=str(cmd_error),
                ),
                ephemeral=True,
            )
            return
        if isinstance(cmd_error, commands.NoPrivateMessage):
            await context.send(
                embed=error(
                    title="Server only",
                    description="This command can only be used in a server.",
                ),
                ephemeral=True,
            )
            return
        raise cmd_error


async def setup(bot) -> None:
    """
    Used to load this cog into a Bot.

    Args:
        bot (DiscordBot): The bot instance to load this cog.
    """
    await bot.add_cog(Setup(bot))
