"""Async streaming-zip pipeline for the download worker.

Composes ``channel.history()`` → per-attachment HTTP chunks → stream-zip's
async generator into a single async iterable that can be fed straight to a
storage backend's ``upload_blob`` without ever materialising the full
archive in memory.

Public surface:

- :class:`AttachmentStreamError` — raised when an attachment's HTTP body
  fails *mid-stream*. Setup-time failures (DNS, 4xx) are skipped silently
  with a warning; once bytes have been emitted the zip is corrupt and the
  job must abort.
- :class:`Counters` — image/video tallies, mutated as the stream is
  consumed; safe to read only after the consumer has fully drained the
  iterable.
- :class:`ZipStreamResult` — pairs the async iterable with its counters.
- :func:`build_zip_stream` — factory returning a ``ZipStreamResult``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from stat import S_IFREG

import aiohttp
import discord
from stream_zip import ZIP_64, async_stream_zip

logger = logging.getLogger("downloader_bot.worker.zip_stream")


class AttachmentStreamError(Exception):
    """Raised when an attachment fails *mid-stream* (after partial bytes emitted).

    Setup-time failures (DNS, connection refused, non-200 status) are
    handled by skipping the attachment in :func:`_members`; this exception
    only fires once we are past the point of no return.
    """


@dataclass
class Counters:
    """Image/video tallies. Mutated during iteration; final after drain."""

    images: int = 0
    videos: int = 0


@dataclass
class ZipStreamResult:
    """The async-iterable zip stream paired with its counters.

    Counters are mutated as ``iterable`` is consumed — read them strictly
    after the consumer has finished draining the iterable.
    """

    iterable: AsyncIterable[bytes]
    counters: Counters


async def _stream_response(
    resp: aiohttp.ClientResponse,
    filename: str,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    """Yield chunks from an already-open response.

    The response is pre-flighted by :func:`_members`, so by the time control
    reaches here we have a valid 200 body and just need to stream it. Any
    error during the body read corrupts the zip, so it surfaces as
    :class:`AttachmentStreamError` (caller aborts the whole job).
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
    allowed_types: set[str],
    counters: Counters,
    chunk_size: int,
):
    """Yield ``(name, mtime, mode, method, chunks)`` tuples for stream-zip.

    Pre-flights each attachment GET *before* yielding the member tuple. If
    setup fails (network error, non-200 status), the attachment is skipped
    cleanly — no member tuple is yielded, so no empty zip entry is left
    behind.
    """
    async for message in channel.history(limit=None):
        for attachment in message.attachments:
            if attachment.content_type not in allowed_types:
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

            if "image" in attachment.content_type:
                counters.images += 1
            elif "video" in attachment.content_type:
                counters.videos += 1

            yield (
                f"{message.id}_{attachment.filename}",
                message.created_at,
                S_IFREG | 0o600,
                ZIP_64,
                _stream_response(resp, attachment.filename, chunk_size),
            )


def build_zip_stream(
    session: aiohttp.ClientSession,
    channel: discord.abc.Messageable,
    allowed_types: set[str],
    chunk_size: int,
) -> ZipStreamResult:
    """Compose the streaming-zip pipeline over a channel's allowed attachments.

    Returns a :class:`ZipStreamResult` whose ``iterable`` can be passed
    directly to a storage backend's ``upload_blob``. The ``counters`` are
    populated lazily as the iterable is drained — read them after the
    upload completes.
    """
    counters = Counters()
    iterable = async_stream_zip(
        _members(session, channel, allowed_types, counters, chunk_size)
    )
    return ZipStreamResult(iterable=iterable, counters=counters)
