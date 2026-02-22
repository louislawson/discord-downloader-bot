from io import BytesIO
import json
import os
from typing import List, Tuple
from zipfile import ZipFile, ZIP_DEFLATED

import discord
from discord.ext import commands
from discord.ext.commands import Context


class Download(commands.Cog, name="download"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.allowed_media_types: List[str] = json.loads(
            os.getenv("ALLOWED_MEDIA_TYPES")
        )

    def is_allowed_media_type(self, media_type: str) -> bool:
        if media_type in self.allowed_media_types:
            return True
        return False

    @commands.hybrid_command(
        name="download",
        description="Download all media in a channel.",
    )
    async def download(self, context: Context) -> None:
        """
        Download all media in a channel.

        :param context: The application command context.
        """
        await context.defer()
        attachments: List[Tuple[str, BytesIO]] = []
        async for message in context.channel.history(limit=None):
            self.bot.logger.info(message.id)
            for attachment in message.attachments:
                self.bot.logger.info(attachment.filename)
                self.bot.logger.info(attachment.content_type)
                self.bot.logger.info(attachment.size)
                if self.is_allowed_media_type(attachment.content_type):
                    att_buffer = BytesIO()
                    await attachment.save(att_buffer)
                    att_buffer.seek(0)
                    attachments.append((attachment.filename, att_buffer))
            self.bot.logger.info("----------------------------")

        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zip_file:
            for filename, file_obj in attachments:
                zip_file.writestr(filename, file_obj.getvalue())
        zip_buffer.seek(0)

        zip_filename = f"{context.channel.name}-media.zip"
        await context.interaction.followup.send(
            file=discord.File(fp=zip_buffer, filename=zip_filename)
        )


async def setup(bot) -> None:
    await bot.add_cog(Download(bot))
