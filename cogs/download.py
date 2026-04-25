"""Download commands cog."""

from datetime import datetime
from io import BytesIO
import json
import os
from typing import List, Tuple
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

        attachments: List[Tuple[str, BytesIO]] = []
        async for message in context.channel.history(limit=None):
            self.bot.logger.debug(message.id)
            for index, attachment in enumerate(message.attachments):
                if self.is_allowed_media_type(attachment.content_type):
                    filename = f"{index}_{attachment.filename}"
                    self.bot.logger.debug(attachment.filename)
                    self.bot.logger.debug(attachment.content_type)
                    self.bot.logger.debug(attachment.size)
                    if "image" in attachment.content_type:
                        image_count += 1
                    if "video" in attachment.content_type:
                        video_count += 1
                    att_buffer = BytesIO()
                    await attachment.save(att_buffer)
                    att_buffer.seek(0)
                    attachments.append((filename, att_buffer))
                self.bot.logger.debug("------------------------------")

        self.bot.logger.debug("Creating zip file of attachments")
        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zip_file:
            for filename, file_obj in attachments:
                self.bot.logger.debug("Adding %s to zipfile", filename)
                zip_file.writestr(filename, file_obj.getvalue())
        zip_buffer.seek(0)

        zip_filename = f"{context.channel.name}-media.zip"

        self.bot.logger.debug("Creating ContainerRepository instance")
        container = ContainerRepository()

        self.bot.logger.debug("Uploading zip file as new blob")
        blob_client = await container.create(
            name=zip_filename,
            data=zip_buffer,
            overwrite=True,
        )
        self.bot.logger.debug("Generating SAS url for blob")
        sas_url = await container.sas_url(blob_client)
        # Required in dev environment as URL changes between Docker and Intranet
        if os.getenv("ENVIRONMENT") == "dev":
            self.bot.logger.debug("Dev environment requires URL change")
            sas_url = sas_url.replace(os.getenv("ST_INT_URL"), os.getenv("ST_EXT_URL"))

        self.bot.logger.debug("Closing zip and file buffers")
        for _, file_obj in attachments:
            file_obj.close()
        zip_buffer.close()

        self.bot.logger.debug("Closing ContainerClient")
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
        embed.set_footer(text=f"Requested by {context.interaction.user}")
        self.bot.logger.debug("Sending followup message")
        await context.interaction.followup.send(embed=embed, ephemeral=only_me)


async def setup(bot) -> None:
    """
    Used to load this cog into a Bot.

    Args:
        bot (DiscordBot): The bot instance to load this cog.
    """
    await bot.add_cog(Download(bot))
