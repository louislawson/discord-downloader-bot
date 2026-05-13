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
    """
    Base Discord embed template.

    Preconfigured with a timestamp.

    Args:
        title (str | None): Title of the embed.
        description (str | None): Optional description/body.
        colour (discord.Colour | None): Optional colour.
        url (str | None): Optional URL.

    Returns:
        discord.Embed: Discord embed.
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
    """
    General use success Discord embed template.

    Preconfigured to be green.

    Args:
        title (str): Title of the embed.
        description (str | None): Optional description/body.
        url (str | None): Optional URL.

    Returns:
        discord.Embed: Discord embed.
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
    """
    General use error Discord embed template.

    Preconfigured to be red.

    Args:
        title (str): Title of the embed.
        description (str | None): Optional description/body.

    Returns:
        discord.Embed: Discord embed.
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
    """
    General use information Discord embed template.

    Preconfigured to be blue.

    Args:
        title (str): Title of the embed.
        description (str | None): Optional description/body.

    Returns:
        discord.Embed: Discord embed.
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
    """
    Media download link Discord embed template.

    Args:
        signed_url (str): The SAS URL to download the archive.
        requester_id (int): The ID of the requesting user.

    Returns:
        discord.Embed: Discord embed.
    """
    embed = success(
        title="Channel Media Download",
        description=f"[Download channel media]({signed_url})",
    )
    embed.set_footer(text=f"Requested by <@{requester_id}> | Job ()")
    return embed


def job_enqueued(
    *,
    task_id: str,
    only_me: bool,
) -> discord.Embed:
    """
    Job enqueued ack Discord embed template.

    Args:
        task_id (str): The ID of the queued task.
        only_me (bool): The Id of the requesting user.

    Returns:
        discord.Embed: Discord embed.
    """
    embed = info(
        title="Download queued",
        description=(
            "Your download has been queued. The result will be sent to "
            "you via DM once it's ready."
            if only_me
            else "Your download has been queued. You'll be notified once it's ready."
        ),
    )
    embed.set_footer(text=f"Job {task_id}")
    return embed
