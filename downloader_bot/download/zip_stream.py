"""Async streaming-zip pipeline for the download worker.

Composes ``channel.history()`` → per-attachment HTTP chunks → stream-zip's
async generator into a single async iterable that can be fed straight to
a storage backend's blob-upload without ever materialising the full
archive in memory.

Public surface:
- ``AttachmentStreamError`` — raised when an attachment's HTTP body fails
  *mid-stream*. Setup-time failures (DNS, 4xx) are skipped silently with a
  warning; once bytes have been emitted the zip is corrupt and the job
  must abort.
- ``NoMatchingAttachments`` — raised when ``channel.history()`` is
  exhausted without yielding a single member tuple (empty channel, all
  attachments filtered by ``allowed_types``, or all pre-flight GETs
  failed). Lets the worker short-circuit and surface a user-facing error
  instead of uploading a valid-but-empty zip.
- ``ProgressSnapshot`` — payload handed to the ``on_progress`` callback
  on every throttled tick.
- ``build_zip_stream`` — factory returning the ``AsyncIterable[bytes]``.
"""

import logging
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from datetime import datetime
from stat import S_IFREG
from typing import TypedDict

import aiohttp
import discord
from stream_zip import ZIP_64, async_stream_zip

logger = logging.getLogger("app.download.zip_stream")


class AttachmentStreamError(Exception):
    """Raised when an attachment fails mid-stream (after partial bytes emitted).

    Setup-time failures (DNS, connection refused, non-200 status) are
    handled by skipping the attachment in ``_members``; this exception
    only fires once we are past the point of no return.
    """


class NoMatchingAttachments(Exception):
    """Raised when the channel yields nothing for the zip pipeline to archive.

    Fires when ``_members`` exhausts without yielding a single tuple —
    truly empty channels, channels whose attachments are all filtered out
    by the ``allowed_types`` set, and channels whose attachments all
    failed pre-flight (CDN 404, DNS, connection refused). The worker
    catches this as a known terminal state and notifies the user instead
    of uploading an empty-central-directory zip.
    """


class ProgressSnapshot(TypedDict):
    """Per-tick progress payload handed to ``on_progress``.

    ``attachments_done`` is the count of matching attachments whose
    member tuple has been yielded (so they're committed to the zip).
    ``bytes_streamed`` is the running sum of ``attachment.size`` for
    those — Discord-reported sizes, not bytes actually sent over the
    wire, but accurate enough for a UI tally.
    ``history_fraction`` is the position of the message *currently being
    walked* within the bounded snowflake range — 0.0 at the start of the
    walk (newest), 1.0 at the end (oldest). ``None`` when either bound
    couldn't be resolved (no ``after`` arg and the oldest-message lookup
    failed) — distinguishes "we don't know" from "we're at the start."
    """

    attachments_done: int
    bytes_streamed: int
    history_fraction: float | None


def _compute_history_fraction(current_id: int, low: int, high: int) -> float:
    """Fraction of the walk completed (0.0 at newest, 1.0 at oldest).

    discord.py walks ``channel.history`` newest-first by default, so the
    first message we see has a snowflake near ``high`` and the last
    near ``low``.
    """
    if high <= low:
        return 1.0
    return max(0.0, min(1.0, (high - current_id) / (high - low)))


async def _resolve_snowflake_bounds(
    channel: discord.abc.Messageable,
    *,
    before: datetime | None,
    after: datetime | None,
) -> tuple[int | None, int | None]:
    """Return ``(low, high)`` snowflakes for the bounded walk.

    ``high`` comes from ``before`` (date→snowflake) or
    ``channel.last_message_id`` (free, already cached on the channel
    object). ``low`` comes from ``after`` (date→snowflake) or one
    ``channel.history(limit=1, oldest_first=True)`` call when the scan
    is fully unbounded — best-effort; if it fails we return ``None`` and
    the caller reports ``history_fraction=0.0`` for the whole walk.
    """
    high = (
        discord.utils.time_snowflake(before, high=True)
        if before is not None
        else getattr(channel, "last_message_id", None)
    )
    if after is not None:
        low = discord.utils.time_snowflake(after, high=False)
    else:
        low = None
        try:
            async for msg in channel.history(limit=1, oldest_first=True):
                low = msg.id
                break
        except Exception as exc:
            logger.warning("oldest-message lookup failed: %s", exc)
            low = None
    return low, high


async def _stream_response(
    resp: aiohttp.ClientResponse,
    filename: str,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    """Yield chunks from an already-open response.

    The response is pre-flighted by ``_members``, so by the time control
    reaches here we have a valid 200 body. Errors during body read corrupt
    the zip, so they surface as ``AttachmentStreamError`` (caller aborts).
    """
    try:
        async for chunk in resp.content.iter_chunked(chunk_size):
            yield chunk
    except aiohttp.ClientError as e:
        raise AttachmentStreamError(
            f"Stream of '{filename}' failed mid-flight: {e}"
        ) from e
    finally:
        # Always release the underlying connection back to the pool, even
        # if the consumer stops iterating early.
        resp.release()


async def _members(
    session: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    matches: Callable[[discord.Attachment, discord.Message], bool] | None,
    chunk_size: int,
    *,
    before: datetime | None = None,
    after: datetime | None = None,
    on_progress: Callable[[ProgressSnapshot], Awaitable[None]] | None = None,
    progress_throttle_seconds: float = 5.0,
):
    """Yield ``(name, mtime, mode, method, chunks)`` tuples for stream-zip.

    Pre-flights each attachment GET *before* yielding the member tuple. If
    setup fails (network error, non-200 status), the attachment is skipped
    cleanly — no member tuple is yielded, so no empty zip entry is left
    behind.

    ``matches=None`` means accept all attachments. ``before`` / ``after``
    are forwarded directly to ``channel.history`` for server-side date
    pruning.

    If ``on_progress`` is provided, it's called on every message scanned
    (throttled to once every ``progress_throttle_seconds`` of wall-clock)
    so filter-stall stretches that yield no attachments still tick the
    heartbeat. The throttle check is synchronous — only the actual emit
    pays coroutine-scheduling cost.
    """
    history_kwargs = {"limit": None}
    if before is not None:
        history_kwargs["before"] = before
    if after is not None:
        history_kwargs["after"] = after

    low_snowflake = high_snowflake = None
    if on_progress is not None:
        low_snowflake, high_snowflake = await _resolve_snowflake_bounds(
            channel,
            before=before,
            after=after,
        )

    attachments_done = 0
    bytes_streamed = 0
    last_emit = time.monotonic()

    async for message in channel.history(**history_kwargs):
        if on_progress is not None:
            now = time.monotonic()
            if (now - last_emit) >= progress_throttle_seconds:
                last_emit = now
                fraction: float | None = (
                    _compute_history_fraction(message.id, low_snowflake, high_snowflake)
                    if low_snowflake is not None and high_snowflake is not None
                    else None
                )
                try:
                    await on_progress(
                        ProgressSnapshot(
                            attachments_done=attachments_done,
                            bytes_streamed=bytes_streamed,
                            history_fraction=fraction,
                        )
                    )
                except Exception as exc:
                    # Progress is best-effort; a Redis blip shouldn't kill
                    # an in-flight zip job. Log and continue.
                    logger.warning("progress callback failed: %s", exc)

        for attachment in message.attachments:
            if matches is not None and not matches(attachment, message):
                continue

            try:
                resp = await session.get(attachment.url).__aenter__()
            except aiohttp.ClientError as e:
                logger.warning(
                    "Skipping attachment '%s' — setup error: %s",
                    attachment.filename,
                    e,
                )
                continue
            if resp.status != 200:
                logger.warning(
                    "Skipping attachment '%s' — HTTP %s",
                    attachment.filename,
                    resp.status,
                )
                resp.release()
                continue

            attachments_done += 1
            bytes_streamed += getattr(attachment, "size", 0) or 0
            yield (
                f"{message.id}_{attachment.filename}",
                message.created_at,
                S_IFREG | 0o600,
                ZIP_64,
                _stream_response(resp, attachment.filename, chunk_size),
            )


async def _peek_or_raise_empty(
    members: AsyncIterator[tuple],
) -> AsyncIterator[tuple]:
    """Re-yield ``members`` but raise ``NoMatchingAttachments`` if it's empty.

    Pulls the first tuple eagerly when the consumer starts iterating; if
    ``members`` exhausts immediately, raises before any bytes are emitted
    downstream. This way ``stream-zip`` never produces an empty-central-
    directory archive, and ``storage.upload_and_sign`` propagates the
    exception out before any blob bytes commit.

    Args:
        members: The async iterator of stream-zip member tuples produced
            by :func:`_members`.

    Yields:
        Each member tuple from ``members``, unchanged and in order.

    Raises:
        NoMatchingAttachments: ``members`` exhausted without yielding a
            single tuple.
    """
    first = None
    async for member in members:
        first = member
        break
    if first is None:
        raise NoMatchingAttachments("channel yielded no attachments to zip")
    yield first
    async for member in members:
        yield member


def build_zip_stream(
    session: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    *,
    matches: Callable[[discord.Attachment, discord.Message], bool] | None = None,
    before: datetime | None = None,
    after: datetime | None = None,
    chunk_size: int = 64 * 1024,
    on_progress: Callable[[ProgressSnapshot], Awaitable[None]] | None = None,
    progress_throttle_seconds: float = 5.0,
) -> AsyncIterable[bytes]:
    """Compose the streaming-zip pipeline over a channel's matching attachments.

    Args:
        session: aiohttp session reused across all attachment GETs.
        channel: Discord channel whose history is walked.
        matches: Predicate invoked per ``(attachment, message)`` to decide
            inclusion. ``None`` accepts every attachment.
        before: Upper-bound timestamp forwarded to ``channel.history`` for
            server-side date pruning. ``None`` means no upper bound.
        after: Lower-bound timestamp forwarded to ``channel.history`` for
            server-side date pruning. ``None`` means no lower bound.
        chunk_size: Bytes per HTTP read from the CDN; also caps in-flight
            memory per attachment.
        on_progress: Optional async callback invoked with a
            ``ProgressSnapshot`` on every message scanned, throttled to
            ``progress_throttle_seconds``. Fires per *message*, not per
            attachment, so filter-stall stretches still tick the heartbeat.
        progress_throttle_seconds: Wall-clock interval between progress
            ticks. Default 5s.

    Returns:
        An async iterable of zip-encoded bytes ready to feed to the
        storage backend's blob-upload.

    Raises:
        NoMatchingAttachments: Raised on the first consumer pull if the
            channel yielded no member tuples (empty, all filtered out,
            or all pre-flight failed). Lets the worker short-circuit
            rather than uploading a useless empty archive.
        AttachmentStreamError: An attachment's HTTP body failed
            mid-stream, after partial bytes were already emitted.
    """
    return async_stream_zip(
        _peek_or_raise_empty(
            _members(
                session,
                channel,
                matches,
                chunk_size,
                before=before,
                after=after,
                on_progress=on_progress,
                progress_throttle_seconds=progress_throttle_seconds,
            )
        )
    )
