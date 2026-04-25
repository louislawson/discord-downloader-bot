"""Download commands cog."""

from datetime import datetime
from io import BytesIO
import json
import os
from typing import List
from zipfile import ZipFile, ZIP_DEFLATED

import discord
from discord.ext import commands
from discord.ext.commands import Context

from storage.container import ContainerRepository


class Download(commands.Cog, name="download"):
    """
    Download commands cog.

    Attributes:
        bot (DiscordBot): DiscordBot instance.
        allowed_media_types (List[str]): List of allowed media types.
    """

    def __init__(self, bot) -> None:
        self.bot = bot
        self.allowed_media_types: List[str] = json.loads(
            os.getenv("ALLOWED_MEDIA_TYPES", "[]")
        )

    def is_allowed_media_type(self, media_type: str) -> bool:
        """
        Tests a content-type against a list of allowed types.

        Args:
            media_type (str): Content-Type to test.

        Returns:
            bool: The result of the test.
        """
        return media_type in self.allowed_media_types

    @commands.hybrid_command(
        name="download",
        description="Download all media in a channel.",
    )
    @commands.max_concurrency(number=1, per=commands.BucketType.channel)
    async def download(self, context: Context, only_me: bool = False) -> None:
        """
        Download all media in a channel.

        Args:
            context (Context): The command context.
            only_me (bool): Only show the download link to you?
        """
        await context.defer(ephemeral=only_me)

        image_count: int = 0
        video_count: int = 0

        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zip_file:
            async for message in context.channel.history(limit=None):
                for attachment in message.attachments:
                    if self.is_allowed_media_type(attachment.content_type):
                        if "image" in attachment.content_type:
                            image_count += 1
                        if "video" in attachment.content_type:
                            video_count += 1
                        att_buffer = BytesIO()
                        await attachment.save(att_buffer)
                        zip_file.writestr(
                            f"{message.id}_{attachment.filename}",
                            att_buffer.getvalue(),
                        )
                        att_buffer.close()

        zip_buffer.seek(0)

        zip_filename = f"{context.channel.name}-media.zip"

        if image_count == 0 and video_count == 0:
            no_media_embed = discord.Embed(
                title="No media found in channel",
                description="No media found in this channel.",
                colour=discord.Color.red(),
                timestamp=datetime.now(),
            )
            await context.send(embed=no_media_embed, ephemeral=only_me)
            return

        self.bot.logger.debug("Creating ContainerRepository instance")
        container = ContainerRepository()
        try:
            self.bot.logger.debug("Uploading zip file as new blob")
            blob_client = await container.create(
                name=zip_filename,
                data=zip_buffer,
                overwrite=True,
            )

            self.bot.logger.debug("Generating SAS url for blob")
            sas_url = await container.sas_url(blob_client)

            if os.getenv("ENVIRONMENT") == "dev":
                sas_url = sas_url.replace(
                    os.getenv("ST_INT_URL"), os.getenv("ST_EXT_URL")
                )

        except Exception:
            self.bot.logger.exception("Failed to upload zip or generate SAS URL")
            error_embed = discord.Embed(
                title="Download failed",
                description="Something went wrong uploading the media. Please try again later.",
                colour=discord.Color.red(),
                timestamp=datetime.now(),
            )
            await context.send(embed=error_embed, ephemeral=only_me)
            return

        finally:
            zip_buffer.close()
            await container.con_client.close()

        self.bot.logger.debug("Formatting embed")
        embed = discord.Embed(
            title="Channel Media Download",
            description=f"[Download channel media]({sas_url})",
            colour=discord.Color.green(),
            timestamp=datetime.now(),
        )
        embed.set_author(name="Downloader Bot")
        embed.add_field(name="Images", value=str(image_count), inline=True)
        embed.add_field(name="Videos", value=str(video_count), inline=True)
        embed.set_footer(text=f"Requested by {context.author}")
        await context.send(embed=embed, ephemeral=only_me)


async def setup(bot) -> None:
    """
    Used to load this cog into a Bot.

    Args:
        bot (DiscordBot): The bot instance to load this cog.
    """
    await bot.add_cog(Download(bot))
