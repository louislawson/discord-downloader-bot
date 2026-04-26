"""Owner commands cog."""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

_VALID_SCOPES = ("global", "guild")


class Owner(commands.Cog, name="owner"):
    """
    Owner commands cog.

    This class contains commands that can only be executed by the owner of the
    Discord bot.

    Attributes:
        bot (DiscordBot): DiscordBot instance.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(
        name="sync",
        description="Synchronises the slash commands.",
    )
    @app_commands.describe(scope="The scope of the sync. Can be `global` or `guild`")
    @commands.is_owner()
    async def sync(self, context: Context, scope: str) -> None:
        """
        Synchronise slash commands either globally or for the current guild.

        Args:
            context (Context): The command context.
            scope (str): The scope of the sync. Must be `global` or `guild`.
        """
        if scope not in _VALID_SCOPES:
            self.bot.logger.warning(
                "%s (ID: %s) passed an unrecognised sync scope: '%s'.",
                context.author,
                context.author.id,
                scope,
            )
            embed = discord.Embed(
                description=f"Unknown scope `{scope}`. Must be one of: `global`, `guild`.",
                color=0xE02B2B,
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
            embed = discord.Embed(
                description="Slash commands have been globally synchronized.",
                color=0xBEBEFE,
            )
            await context.send(embed=embed)

        elif scope == "guild":
            if context.guild is None:
                embed = discord.Embed(
                    description="Guild sync can only be run inside a server, not in DMs.",
                    color=0xE02B2B,
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
            embed = discord.Embed(
                description="Slash commands have been synchronized in this guild.",
                color=0xBEBEFE,
            )
            await context.send(embed=embed)


async def setup(bot) -> None:
    """
    Used to load this cog into a Bot.

    Args:
        bot (DiscordBot): The bot instance to load this cog.
    """
    await bot.add_cog(Owner(bot))
