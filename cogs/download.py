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
            for attachment in message.attachments:
                if self.is_allowed_media_type(attachment.content_type):
                    att_buffer = BytesIO()
                    await attachment.save(att_buffer)
                    att_buffer.seek(0)
                    attachments.append((attachment.filename, att_buffer))

        zip_buffer = BytesIO()
        with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zip_file:
            for filename, file_obj in attachments:
                zip_file.writestr(filename, file_obj.getvalue())
        zip_buffer.seek(0)

        zip_filename = f"{context.channel.name}-media.zip"

        container = ContainerRepository()

        blob_client = await container.create(
            name=zip_filename,
            data=zip_buffer,
            overwrite=True,
        )
        sas_url = await container.sas_url(blob_client)
        # Required in dev environment as URL changes between Docker and Intranet
        if os.getenv("ENVIRONMENT") == "dev":
            sas_url = sas_url.replace(
                os.getenv("ST_INT_URL"), os.getenv("ST_EXT_URL")
            )

        await container.con_client.close()

        embed = discord.Embed(
            title="Channel Media",
            description=f"Click to download channel media {sas_url}",
            color=discord.Color.green(),
        )
        await context.interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Download(bot))
