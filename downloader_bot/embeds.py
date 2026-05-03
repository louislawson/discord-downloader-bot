from datetime import UTC, datetime

import discord


def _base(
    *,
    title: str | None = None,
    description: str | None = None,
    colour: discord.Colour | None = None,
    url: str | None = None,
) -> discord.Embed:
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
    return _base(
        title=title,
        description=description,
        colour=discord.Color.blurple(),
    )


def media_download(
    *,
    signed_url: str,
    image_count: int,
    video_count: int,
    requester: str,
) -> discord.Embed:
    embed = success(
        title="Channel Media Download",
        description=f"[Download channel media]({signed_url})",
    )
    embed.add_field(name="Images", value=str(image_count), inline=True)
    embed.add_field(name="Videos", value=str(video_count), inline=True)
    embed.set_footer(text=f"Requested by {requester}")
    return embed
