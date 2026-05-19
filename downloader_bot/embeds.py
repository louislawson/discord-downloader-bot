"""Downloader Bot Discord embeds."""

from datetime import UTC, datetime
from typing import Literal

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


def _humanise_bytes(n: int) -> str:
    """Render a byte count as ``X.Y GB`` / ``X.Y MB`` / ``X.Y KB`` / ``N bytes``."""
    if n >= 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024 * 1024):.1f} GB"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} bytes"


_JOB_PHASE_LABELS: dict[str, str] = {
    "picked_up": "Worker picked it up — starting",
    "queued": "Queued (waiting for a worker)",
    "stream": "Streaming attachments",
    "deliver": "Uploading & delivering",
    "done": "Delivered",
    "cancelled": "Cancelled",
}


def job_status(
    *,
    task_id: str,
    phase: Literal["picked_up", "queued", "stream", "deliver", "done", "cancelled"],
    requester_id: int,
    attachments_done: int | None = None,
    bytes_streamed: int | None = None,
    history_fraction: float | None = None,
    heartbeat_age_seconds: float | None = None,
) -> discord.Embed:
    """Build the ``/status`` read embed.

    Colour is picked from ``phase`` (cancelled → red, done → green,
    everything in-flight → blurple). Rendering uses ``add_field`` rather
    than a long description line so the mobile Discord client doesn't
    wrap mid-word.

    Args:
        task_id: The Taskiq task ID, rendered in the footer.
        phase: Current phase as reported by the task's progress meta,
            or ``"queued"`` when no progress exists yet, or ``"cancelled"``
            when the cancellation backend reports the job cancelled.
        requester_id: Discord user ID of the requester (footer mention).
        attachments_done: Optional running count of attachments included
            in the zip so far.
        bytes_streamed: Optional running byte total for those attachments.
        history_fraction: Optional snowflake-position estimate in
            ``[0.0, 1.0]`` — rendered as "~X% through channel history."
        heartbeat_age_seconds: Optional seconds since the last progress
            tick — rendered as "Xs ago" to distinguish stuck from slow.

    Returns:
        A discord.Embed with conditional fields for whatever meta is
        available.
    """
    if phase == "cancelled":
        embed = error(title="Job cancelled")
    elif phase == "done":
        embed = success(title="Job delivered")
    else:
        embed = info(title="Job status")

    embed.add_field(
        name="Phase",
        value=_JOB_PHASE_LABELS.get(phase, "Unknown phase"),
        inline=False,
    )

    if heartbeat_age_seconds is not None:
        embed.add_field(
            name="Heartbeat",
            value=f"{int(heartbeat_age_seconds)}s ago",
            inline=True,
        )
    if history_fraction is not None:
        embed.add_field(
            name="Position",
            value=f"~{int(history_fraction * 100)}% through channel history",
            inline=True,
        )
    if attachments_done is not None:
        embed.add_field(
            name="Tally",
            value=(
                f"{attachments_done} attachments, "
                f"{_humanise_bytes(bytes_streamed or 0)}"
            ),
            inline=True,
        )

    embed.set_footer(text=f"Job {task_id} • requested by <@{requester_id}>")
    return embed


def job_cancelled(*, task_id: str, requester_id: int) -> discord.Embed:
    """Build the ``/cancel`` confirmation embed.

    Does not mention rate-limit refund — whether a token was returned is
    an internal bookkeeping detail; users don't know what the bucket is.

    Args:
        task_id: The Taskiq task ID, rendered in the footer.
        requester_id: Discord user ID of the requester (footer mention).
    """
    embed = error(
        title="Cancellation requested",
        description=("The worker will stop and clean up any partial archive."),
    )
    embed.set_footer(text=f"Job {task_id} • requested by <@{requester_id}>")
    return embed


def job_not_found(*, task_id: str | None = None) -> discord.Embed:
    """Build the generic "not found" embed for ``/status`` and ``/cancel``.

    Deliberately collapses 404 (no such task) and 403 (not yours) into a
    single response so a leaked task ID from a public channel ack
    footer can't be used to probe for existence.

    Args:
        task_id: When ``None``, render as "no active jobs" (the user
            ran ``/status`` with no args). Otherwise render as "job not
            found" without distinguishing missing from unauthorised.
    """
    if task_id is None:
        return error(
            title="No active jobs",
            description="You don't have any active downloads to look at.",
        )
    return error(
        title="Job not found",
        description=(
            "That job doesn't exist, has already finished, or you don't have "
            "access to it."
        ),
    )
