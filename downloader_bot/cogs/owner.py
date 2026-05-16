"""Owner commands cog."""

from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from downloader_bot.embeds import error, info

_VALID_SCOPES = ("global", "guild")


class Owner(commands.Cog, name="owner"):
    """Bot-owner-only commands (prefix-only)."""

    def __init__(self, bot) -> None:
        """Bind the cog to its parent bot."""
        self.bot = bot

    @commands.command(
        name="sync",
        description="Synchronises the slash commands.",
    )
    @app_commands.describe(scope="The scope of the sync. Can be `global` or `guild`")
    @commands.is_owner()
    async def sync(self, context: Context, scope: str) -> None:
        """Re-register slash commands globally or for the current guild.

        Run this after adding or changing a hybrid command before the
        slash UI reflects the change.

        Args:
            context: The command context.
            scope: ``global`` or ``guild``.
        """
        if scope not in _VALID_SCOPES:
            self.bot.logger.warning(
                "%s (ID: %s) passed an unrecognised sync scope: '%s'.",
                context.author,
                context.author.id,
                scope,
            )
            embed = error(
                title="Command error",
                description=f"Unknown scope `{scope}`. Must be one of: `global`, `guild`.",
            )
            await context.send(embed=embed)
            return

        if scope == "global":
            await context.bot.tree.sync()
            self.bot.logger.info(
                "Slash commands globally synchronised by %s (ID: %s).",
                context.author,
                context.author.id,
            )
            embed = info(
                title="Command sync",
                description="Slash commands have been globally synchronized.",
            )
            await context.send(embed=embed)

        elif scope == "guild":
            if context.guild is None:
                embed = error(
                    title="Command error",
                    description="Guild sync can only be run inside a server, not in DMs.",
                )
                await context.send(embed=embed)
                return

            context.bot.tree.copy_global_to(guild=context.guild)
            await context.bot.tree.sync(guild=context.guild)
            self.bot.logger.info(
                "Slash commands synchronised to guild '%s' (ID: %s) by %s (ID: %s).",
                context.guild.name,
                context.guild.id,
                context.author,
                context.author.id,
            )
            embed = info(
                title="Command sync",
                description="Slash commands have been synchronized in this guild.",
            )
            await context.send(embed=embed)


async def setup(bot) -> None:
    """Extension entry point; called by ``bot.load_extension``.

    Args:
        bot: The bot instance to load this cog into.
    """
    await bot.add_cog(Owner(bot))
