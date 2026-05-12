"""Per-guild setup command.

Server-owner-only. A single hybrid command that overwrites the guild's
``delivery_mode`` / ``results_channel_id`` / ``retention_hours`` in one
shot via ``GuildSettingsRepo.upsert``. ``allowed_media_types`` and
``max_archive_size_bytes`` are left at their defaults until separate
commands exist for them.
"""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context, errors

from downloader_bot.db.guild_settings import GuildSettings
from downloader_bot.embeds import error, success

_VALID_MODES = ("dm", "channel")


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
    """Per-guild configuration commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="setup",
        description="Configure download delivery for this server.",
    )
    @commands.guild_only()
    @_is_guild_owner()
    @app_commands.describe(
        delivery_mode="`dm` = private DM to the requester | `channel` = post in the configured channel",
        results_channel="Required for `channel` mode — where results get posted.",
        retention_hours="How many hours generated download links remain valid (default 24).",
    )
    async def setup_cmd(
        self,
        context: Context,
        delivery_mode: str,
        results_channel: discord.TextChannel | None = None,
        retention_hours: int = 24,
    ) -> None:
        """Overwrite this guild's delivery settings.

        Args:
            context: The command context.
            delivery_mode: ``dm`` or ``channel``.
            results_channel: The channel to post results in. Required when
                ``delivery_mode == "channel"``; ignored otherwise.
            retention_hours: SAS URL lifetime, in hours.
        """
        # Manual validation: discord.py's slash UI uses Literal for choices,
        # but the callback can still be invoked with arbitrary strings (e.g.
        # from prefix commands or unit tests), so we re-check here and emit
        # a friendly embed instead of letting it fall to the global handler.
        if delivery_mode not in _VALID_MODES:
            await context.send(
                embed=error(
                    title="Invalid delivery mode",
                    description=(
                        f"`delivery_mode` must be one of: "
                        f"{', '.join(f'`{m}`' for m in _VALID_MODES)}."
                    ),
                ),
                ephemeral=True,
            )
            return

        # Schema invariant: delivery_mode='channel' requires a channel id.
        # Reject early so the user sees a precise error rather than a DB
        # IntegrityError surfaced as "Unexpected error".
        if delivery_mode == "channel" and results_channel is None:
            await context.send(
                embed=error(
                    title="Missing channel",
                    description="`channel` mode requires a `results_channel` argument.",
                ),
                ephemeral=True,
            )
            return

        await self.bot.guild_settings_repo.upsert(
            GuildSettings(
                guild_id=context.guild.id,
                delivery_mode=delivery_mode,
                results_channel_id=(
                    results_channel.id if results_channel is not None else None
                ),
                retention_hours=retention_hours,
            ),
        )

        channel_str = (
            results_channel.mention if results_channel is not None else "_not set_"
        )
        await context.send(
            embed=success(
                title="Settings updated",
                description=(
                    f"**Mode:** `{delivery_mode}`\n"
                    f"**Channel:** {channel_str}\n"
                    f"**Retention:** `{retention_hours}h`"
                ),
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
    """Load this cog into a bot."""
    await bot.add_cog(Setup(bot))
