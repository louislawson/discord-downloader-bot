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
- ``build_zip_stream`` — factory returning the ``AsyncIterable[bytes]``.
"""

import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable
from datetime import datetime
from stat import S_IFREG

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
):
    """Yield ``(name, mtime, mode, method, chunks)`` tuples for stream-zip.

    Pre-flights each attachment GET *before* yielding the member tuple. If
    setup fails (network error, non-200 status), the attachment is skipped
    cleanly — no member tuple is yielded, so no empty zip entry is left
    behind.

    ``matches=None`` means accept all attachments. ``before`` / ``after``
    are forwarded directly to ``channel.history`` for server-side date
    pruning.
    """
    history_kwargs = {"limit": None}
    if before is not None:
        history_kwargs["before"] = before
    if after is not None:
        history_kwargs["after"] = after

    async for message in channel.history(**history_kwargs):
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
            )
        )
    )
