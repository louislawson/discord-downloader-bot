"""Download commands cog."""

from datetime import datetime
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import discord
from discord.ext import commands
from discord.ext.commands import Context

from config import settings
from storage.container import ContainerRepository
from storage.exceptions import BlobUploadError, ContainerConfigError, SasGenerationError


def _guild_upload_limit(guild: discord.Guild | None) -> int:
    """
    Return the file upload limit in bytes for a given guild.

    Discord's limit scales with the guild's boost tier:
      - Tier 0/1: 8 MB
      - Tier 2:  50 MB
      - Tier 3: 100 MB

    Args:
        guild (Guild | None): The guild, or None if in a DM.

    Returns:
        int: The upload limit in bytes.
    """
    if guild is None:
        return 8 * 1024 * 1024  # DMs follow the default limit

    tier = guild.premium_tier
    if tier >= 3:
        return 100 * 1024 * 1024
    if tier == 2:
        return 50 * 1024 * 1024
    return 8 * 1024 * 1024


def _error_embed(title: str, description: str) -> discord.Embed:
    """
    Build a consistently styled error embed.

    Args:
        title (str): The embed title.
        description (str): The embed description shown to the user.

    Returns:
        discord.Embed: A red-coloured error embed.
    """
    return discord.Embed(
        title=title,
        description=description,
        colour=discord.Color.red(),
        timestamp=datetime.now(),
    )


async def _send_as_attachment(
    context: Context,
    zip_buffer: BytesIO,
    zip_filename: str,
    image_count: int,
    video_count: int,
    only_me: bool,
) -> None:
    """
    Send the zip as a direct Discord attachment with a warning embed.

    Used as a fallback when Azure storage is unavailable. Rewinds the buffer
    before sending so it can be re-read after a failed upload attempt.

    Args:
        context (Context): The command context.
        zip_buffer (BytesIO): In-memory zip archive.
        zip_filename (str): Filename to use for the Discord attachment.
        image_count (int): Number of images in the archive.
        video_count (int): Number of videos in the archive.
        only_me (bool): Whether the response should be ephemeral.
    """
    zip_buffer.seek(0)
    embed = discord.Embed(
        title="Channel Media Download (direct attachment)",
        description=(
            "Azure storage was unavailable, so the archive is attached directly. "
            "Note: this file will expire when Discord removes the attachment."
        ),
        colour=discord.Color.orange(),
        timestamp=datetime.now(),
    )
    embed.set_author(name="Downloader Bot")
    embed.add_field(name="Images", value=str(image_count), inline=True)
    embed.add_field(name="Videos", value=str(video_count), inline=True)
    embed.set_footer(text=f"Requested by {context.author}")
    await context.send(
        embed=embed,
        file=discord.File(zip_buffer, filename=zip_filename),
        ephemeral=only_me,
    )


class Download(commands.Cog, name="download"):
    """
    Download commands cog.

    Attributes:
        bot (DiscordBot): DiscordBot instance.
        allowed_media_types (list[str]): List of allowed media types.
    """

    def __init__(self, bot) -> None:
        self.bot = bot
        self.allowed_media_types: list[str] = settings.ALLOWED_MEDIA_TYPES

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

        # --- Phase 1: collect and zip attachments ---
        zip_buffer = BytesIO()
        try:
            with ZipFile(zip_buffer, mode="w", compression=ZIP_DEFLATED) as zip_file:
                async for message in context.channel.history(limit=None):
                    for attachment in message.attachments:
                        if not self.is_allowed_media_type(attachment.content_type):
                            continue

                        if "image" in attachment.content_type:
                            image_count += 1
                        if "video" in attachment.content_type:
                            video_count += 1

                        att_buffer = BytesIO()
                        try:
                            await attachment.save(att_buffer)
                        except discord.HTTPException as e:
                            # A single attachment failing shouldn't abort the whole run;
                            # log it and move on so the user still gets the rest.
                            self.bot.logger.warning(
                                "Skipped attachment '%s' (message ID: %s) — could not download: %s",
                                attachment.filename,
                                message.id,
                                e,
                            )
                            att_buffer.close()
                            continue

                        zip_file.writestr(
                            f"{message.id}_{attachment.filename}",
                            att_buffer.getvalue(),
                        )
                        att_buffer.close()

        except discord.Forbidden:
            self.bot.logger.warning(
                "Missing read permissions for channel '%s' (ID: %s) — download aborted.",
                context.channel.name,
                context.channel.id,
            )
            await context.send(
                embed=_error_embed(
                    "Missing permissions",
                    "I don't have permission to read the history of this channel.",
                ),
                ephemeral=only_me,
            )
            zip_buffer.close()
            return

        except discord.HTTPException as e:
            self.bot.logger.exception(
                "Unexpected Discord API error while reading channel history: %s", e
            )
            await context.send(
                embed=_error_embed(
                    "Discord error",
                    "An unexpected Discord error occurred while reading this channel's history. "
                    "Please try again later.",
                ),
                ephemeral=only_me,
            )
            zip_buffer.close()
            return

        # --- Phase 2: nothing to zip ---
        if image_count == 0 and video_count == 0:
            await context.send(
                embed=_error_embed(
                    "No media found",
                    "No allowed media types were found in this channel.",
                ),
                ephemeral=only_me,
            )
            zip_buffer.close()
            return

        zip_buffer.seek(0)
        zip_filename = f"{context.channel.name}-media.zip"

        # --- Phase 3: upload to Azure and generate SAS URL ---
        # On certain failures (BlobUploadError, SasGenerationError) we fall back
        # to sending the zip as a direct Discord attachment if it fits within the
        # guild's upload limit. ContainerConfigError is a deployment problem and
        # not recoverable at runtime, so it still shows a hard error.
        upload_limit = _guild_upload_limit(context.guild)
        zip_size = zip_buffer.getbuffer().nbytes

        self.bot.logger.debug("Creating ContainerRepository instance")
        try:
            async with ContainerRepository() as container:
                self.bot.logger.debug("Uploading zip file as new blob")
                blob_client = await container.create(
                    name=zip_filename,
                    data=zip_buffer,
                    overwrite=True,
                )

                self.bot.logger.debug("Generating SAS URL for blob")
                sas_url = await container.sas_url(
                    blob_name=blob_client.blob_name,
                    blob_url=blob_client.url,
                )

                if settings.ENVIRONMENT == "dev":
                    sas_url = sas_url.replace(
                        settings.ST_INT_URL, settings.ST_EXT_URL
                    )

        except ContainerConfigError:
            self.bot.logger.exception(
                "Storage is misconfigured — cannot upload zip for channel '%s'.",
                context.channel.name,
            )
            await context.send(
                embed=_error_embed(
                    "Storage misconfigured",
                    "The bot's storage backend is not configured correctly. "
                    "Please contact an administrator.",
                ),
                ephemeral=only_me,
            )
            zip_buffer.close()
            return

        except (BlobUploadError, SasGenerationError) as e:
            self.bot.logger.exception(
                "Azure storage failed for channel '%s' (%s) — attempting Discord attachment fallback.",
                context.channel.name,
                type(e).__name__,
            )

            if zip_size <= upload_limit:
                self.bot.logger.debug(
                    "Zip size %d bytes is within the %d-byte guild limit — sending as attachment.",
                    zip_size,
                    upload_limit,
                )
                try:
                    await _send_as_attachment(
                        context, zip_buffer, zip_filename, image_count, video_count, only_me
                    )
                except discord.HTTPException as attach_err:
                    self.bot.logger.exception(
                        "Discord attachment fallback also failed for channel '%s': %s",
                        context.channel.name,
                        attach_err,
                    )
                    await context.send(
                        embed=_error_embed(
                            "Download failed",
                            "The media archive could not be uploaded to storage or sent as an attachment. "
                            "Please try again later.",
                        ),
                        ephemeral=only_me,
                    )
                finally:
                    zip_buffer.close()
            else:
                self.bot.logger.warning(
                    "Zip size %d bytes exceeds the %d-byte guild limit — cannot fall back to attachment.",
                    zip_size,
                    upload_limit,
                )
                await context.send(
                    embed=_error_embed(
                        "Upload failed",
                        "The media archive could not be uploaded to storage, and it is too large "
                        f"({zip_size // (1024 * 1024)} MB) to send as a Discord attachment. "
                        "Please try again later or contact an administrator.",
                    ),
                    ephemeral=only_me,
                )
                zip_buffer.close()

            return

        finally:
            # Only reached on the happy path — buffer is closed in fallback branches above.
            if not zip_buffer.closed:
                zip_buffer.close()

        # --- Phase 4: send the success embed ---
        self.bot.logger.debug("Formatting success embed")
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
