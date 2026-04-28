"""General commands cog."""

import discord
from discord.ext import commands
from discord.ext.commands import Context


class General(commands.Cog, name="general"):
    """
    General commands cog.

    Attributes:
        bot (DiscordBot): DiscordBot instance.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="invite",
        description="Get the invite link of the bot.",
    )
    async def invite(self, context: Context) -> None:
        """
        Get the invite link of the bot.

        Args:
            context (Context): The command context.
        """
        embed = discord.Embed(
            description=f"Invite me by clicking [here]({self.bot.invite_link}).",
        )
        try:
            await context.author.send(embed=embed)
            await context.send("I sent you a private message!")
        except discord.Forbidden:
            await context.send(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    """
    Used to load this cog into a Bot.

    Args:
        bot (DiscordBot): The bot instance to load this cog.
    """
    await bot.add_cog(General(bot))
