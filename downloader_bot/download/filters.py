"""Per-invocation filters for ``/download``.

Drives narrowing of the channel-history walk and per-attachment MIME
filtering. The cog parses and validates user input, then passes a
JSON-serialisable ``DownloadFilters`` dict to the Taskiq task; the task
calls :func:`resolve` to turn that dict into a :class:`ResolvedFilters`
(a callable matcher + the ``before`` / ``after`` datetimes to forward
to ``channel.history``).

The category filter is **intersected** with the guild's
``allowed_media_types`` — users can narrow but not bypass admin policy.
``during`` is mutually exclusive with ``before`` / ``after``; the cog
guards that before enqueueing.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypedDict

import discord

from downloader_bot.db.guild_settings import GuildSettings


class DownloadFilters(TypedDict, total=False):
    """JSON-serialisable filter struct passed cog → task.

    Every key is optional (``total=False``); an absent or ``None`` value
    means "no constraint for this dimension". This shape survives the
    Taskiq AMQP serialiser without custom adapters.
    """

    category: str | None
    from_user_id: int | None
    before: str | None
    after: str | None
    during: str | None


class FilterParseError(ValueError):
    """User-friendly validation error surfaced by the cog as a red embed."""


CATEGORY_NAMES: tuple[str, ...] = ("image", "video", "audio", "gif", "other")


def _is_image(mime: str) -> bool:
    return mime.startswith("image/") and mime != "image/gif"


def _is_video(mime: str) -> bool:
    return mime.startswith("video/")


def _is_audio(mime: str) -> bool:
    return mime.startswith("audio/")


def _is_gif(mime: str) -> bool:
    return mime == "image/gif"


def _is_other(mime: str) -> bool:
    return not (_is_image(mime) or _is_video(mime) or _is_audio(mime) or _is_gif(mime))


CATEGORY_PREDICATES: dict[str, Callable[[str], bool]] = {
    "image": _is_image,
    "video": _is_video,
    "audio": _is_audio,
    "gif": _is_gif,
    "other": _is_other,
}


_DURATION_RE = re.compile(r"^\s*(\d+)([mhdw])\s*$")
_UNIT_TO_SECONDS: dict[str, int] = {
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_duration(value: str) -> timedelta:
    """Parse a relative duration like ``7d`` / ``3w`` / ``2h`` / ``30m``.

    Args:
        value: The duration string. Surrounding whitespace is tolerated.
            The unit must be one of ``m`` (minutes), ``h`` (hours),
            ``d`` (days), ``w`` (weeks).

    Returns:
        The parsed positive ``timedelta``.

    Raises:
        FilterParseError: ``value`` did not match the grammar or its
            magnitude was zero.
    """
    match = _DURATION_RE.match(value)
    if match is None:
        raise FilterParseError(
            f"Invalid duration `{value}`. Use a number followed by "
            "`m`, `h`, `d`, or `w` (e.g. `30m`, `2h`, `7d`, `3w`)."
        )
    magnitude = int(match.group(1))
    if magnitude == 0:
        raise FilterParseError(
            f"Invalid duration `{value}` — magnitude must be greater than zero."
        )
    return timedelta(seconds=magnitude * _UNIT_TO_SECONDS[match.group(2)])


NAMED_PERIODS: tuple[str, ...] = (
    "today",
    "yesterday",
    "this-week",
    "last-week",
    "this-month",
    "last-month",
    "this-year",
    "last-year",
)


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_week(dt: datetime) -> datetime:
    return _start_of_day(dt) - timedelta(days=dt.weekday())


def _start_of_month(dt: datetime) -> datetime:
    return _start_of_day(dt).replace(day=1)


def _start_of_year(dt: datetime) -> datetime:
    return _start_of_day(dt).replace(month=1, day=1)


def _previous_month_start(dt: datetime) -> datetime:
    return _start_of_month(_start_of_month(dt) - timedelta(days=1))


def _previous_year_start(dt: datetime) -> datetime:
    return _start_of_year(dt).replace(year=dt.year - 1)


def resolve_named_period(name: str, now: datetime) -> tuple[datetime, datetime]:
    """Resolve a named period to ``(after_dt, before_dt)`` boundaries.

    ``after_dt`` is the inclusive start of the window; ``before_dt`` is
    the exclusive end. The pair feeds ``channel.history(before=, after=)``
    directly.

    Args:
        name: One of :data:`NAMED_PERIODS`.
        now: "Current time" anchor; should be UTC-aware.

    Returns:
        The ``(after_dt, before_dt)`` pair.

    Raises:
        FilterParseError: ``name`` is not a known period.
    """
    if name == "today":
        return _start_of_day(now), now
    if name == "yesterday":
        today = _start_of_day(now)
        return today - timedelta(days=1), today
    if name == "this-week":
        return _start_of_week(now), now
    if name == "last-week":
        this_week = _start_of_week(now)
        return this_week - timedelta(weeks=1), this_week
    if name == "this-month":
        return _start_of_month(now), now
    if name == "last-month":
        this_month = _start_of_month(now)
        return _previous_month_start(now), this_month
    if name == "this-year":
        return _start_of_year(now), now
    if name == "last-year":
        return _previous_year_start(now), _start_of_year(now)
    raise FilterParseError(
        f"Unknown named period `{name}`. Expected one of: {', '.join(NAMED_PERIODS)}."
    )


@dataclass(frozen=True, slots=True)
class ResolvedFilters:
    """Outcome of :func:`resolve`: a matcher and the date bounds."""

    matches: Callable[[discord.Attachment, discord.Message], bool]
    before: datetime | None
    after: datetime | None


def _content_type(attachment: discord.Attachment) -> str:
    return (attachment.content_type or "").split(";", 1)[0].strip().lower()


def _build_matcher(
    *,
    category: str | None,
    from_user_id: int | None,
    guild_allowed_types: set[str] | None,
) -> Callable[[discord.Attachment, discord.Message], bool]:
    """Compose the per-attachment matcher from category, author, and guild policy."""
    category_pred = CATEGORY_PREDICATES.get(category) if category else None

    def _matches(attachment: discord.Attachment, message: discord.Message) -> bool:
        if from_user_id is not None and message.author.id != from_user_id:
            return False
        mime = _content_type(attachment)
        if guild_allowed_types is not None and mime not in guild_allowed_types:
            return False
        return category_pred is None or category_pred(mime)

    return _matches


def resolve(
    filters: DownloadFilters | None,
    guild_settings: GuildSettings,
    *,
    now: datetime,
) -> ResolvedFilters:
    """Turn a ``DownloadFilters`` dict and guild policy into a ``ResolvedFilters``.

    Args:
        filters: The filter payload from the cog, or ``None`` for "no
            user-supplied filters".
        guild_settings: The per-guild settings; ``allowed_media_types``
            is intersected with the user-supplied category filter.
        now: "Current time" anchor for relative durations and named
            periods. Should be UTC-aware.

    Returns:
        A :class:`ResolvedFilters` with a matcher and the date bounds.

    Raises:
        FilterParseError: A relative duration or named period in
            ``filters`` failed to parse. The cog catches this before
            enqueueing, so the task body should not see it in practice.
    """
    payload = filters or {}
    category = payload.get("category")
    from_user_id = payload.get("from_user_id")
    before_value = payload.get("before")
    after_value = payload.get("after")
    during = payload.get("during")

    guild_allowed = (
        set(guild_settings.allowed_media_types)
        if guild_settings.allowed_media_types is not None
        else None
    )
    matcher = _build_matcher(
        category=category,
        from_user_id=from_user_id,
        guild_allowed_types=guild_allowed,
    )

    if during is not None:
        after_dt, before_dt = resolve_named_period(during, now)
    else:
        before_dt = now - parse_duration(before_value) if before_value else None
        after_dt = now - parse_duration(after_value) if after_value else None

    return ResolvedFilters(matches=matcher, before=before_dt, after=after_dt)
