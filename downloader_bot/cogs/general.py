"""General commands cog."""

import discord
from discord.ext import commands
from discord.ext.commands import Context

from downloader_bot.embeds import info


class General(commands.Cog, name="general"):
    """General-purpose commands available in every server (and DMs)."""

    def __init__(self, bot) -> None:
        """Bind the cog to its parent bot."""
        self.bot = bot

    @commands.hybrid_command(
        name="invite",
        description="Get the invite link of the bot.",
    )
    async def invite(self, context: Context) -> None:
        """Send the requester an embed with the bot's invite link.

        DMs the embed first; falls back to an ephemeral channel reply if
        the user has DMs disabled.

        Args:
            context: The command context.
        """
        embed = info(
            title="Bot Invite",
            description=f"Invite me by clicking [here]({self.bot.invite_link}).",
        )
        try:
            await context.author.send(embed=embed)
            await context.send("I sent you a private message!")
        except discord.Forbidden:
            await context.send(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    """Extension entry point; called by ``bot.load_extension``.

    Args:
        bot: The bot instance to load this cog into.
    """
    await bot.add_cog(General(bot))
