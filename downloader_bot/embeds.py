"""Downloader Bot Discord embeds."""

from datetime import UTC, datetime

import discord


def _base(
    *,
    title: str | None = None,
    description: str | None = None,
    colour: discord.Colour | None = None,
    url: str | None = None,
) -> discord.Embed:
    """Build a base embed with a UTC timestamp and the bot's author block.

    Args:
        title: Title of the embed.
        description: Optional description/body.
        colour: Optional accent colour.
        url: Optional URL for the title.

    Returns:
        A discord.Embed ready to be sent.
    """
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour,
        url=url,
        timestamp=datetime.now(UTC),
    )
    embed.set_author(name="Downloader Bot")
    return embed


def success(
    *,
    title: str,
    description: str | None = None,
    url: str | None = None,
) -> discord.Embed:
    """Build a green success embed.

    Args:
        title: Title of the embed.
        description: Optional description/body.
        url: Optional URL for the title.

    Returns:
        A green-accented discord.Embed.
    """
    return _base(
        title=title,
        description=description,
        colour=discord.Color.green(),
        url=url,
    )


def error(
    *,
    title: str,
    description: str | None = None,
) -> discord.Embed:
    """Build a red error embed.

    Args:
        title: Title of the embed.
        description: Optional description/body.

    Returns:
        A red-accented discord.Embed.
    """
    return _base(
        title=title,
        description=description,
        colour=discord.Color.red(),
    )


def info(
    *,
    title: str,
    description: str | None = None,
) -> discord.Embed:
    """Build a blurple informational embed.

    Args:
        title: Title of the embed.
        description: Optional description/body.

    Returns:
        A blurple-accented discord.Embed.
    """
    return _base(
        title=title,
        description=description,
        colour=discord.Color.blurple(),
    )


def media_download(
    *,
    signed_url: str,
    requester_id: int,
) -> discord.Embed:
    """Build the embed delivered to the user with the archive download link.

    Args:
        signed_url: The pre-signed URL the user clicks to download the archive.
        requester_id: Discord user ID of the requester, rendered as a mention
            in the footer.

    Returns:
        A green discord.Embed linking to the archive.
    """
    embed = success(
        title="Channel Media Download",
        description=f"[Download channel media]({signed_url})",
    )
    embed.set_footer(text=f"Requested by <@{requester_id}> | Job ()")
    return embed


def no_attachments(*, requester_id: int) -> discord.Embed:
    """Build the error embed shown when a /download channel had nothing to archive.

    Args:
        requester_id: Discord user ID of the requester, rendered as a mention
            in the footer.

    Returns:
        A red discord.Embed explaining the empty result.
    """
    embed = error(
        title="No media found",
        description=(
            "This channel has no attachments to archive — either it's empty, "
            "the messages contain no files, or the server's allowed media-type "
            "filter excluded everything."
        ),
    )
    embed.set_footer(text=f"Requested by <@{requester_id}>")
    return embed


def job_enqueued(
    *,
    task_id: str,
    dm_me: bool,
) -> discord.Embed:
    """Build the ack embed shown when a /download is accepted onto the queue.

    Args:
        task_id: The Taskiq task ID, rendered in the footer for support traceability.
        dm_me: ``True`` when the requester chose DM-only delivery. Drives the
            description wording.

    Returns:
        A blurple discord.Embed acknowledging the queued job.
    """
    embed = info(
        title="Download queued",
        description=(
            "Your download has been queued. The result will be sent to "
            "you via DM once it's ready."
            if dm_me
            else "Your download has been queued. You'll be notified once it's ready."
        ),
    )
    embed.set_footer(text=f"Job {task_id}")
    return embed
