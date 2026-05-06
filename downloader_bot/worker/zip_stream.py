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

The response lifecycle is owned by the ``async with session.get(...)`` in
:func:`_members`, which spans the chunk drain. Cancellation between the
member yield and the consumer's drain releases the connection cleanly
rather than leaking it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from pathlib import PureWindowsPath
from stat import S_IFREG

import aiohttp
import discord
from stream_zip import ZIP_64, async_stream_zip

logger = logging.getLogger("downloader_bot.worker.zip_stream")


def _safe_filename(filename: str) -> str:
    """Strip path components and disallowed names from an attachment filename.

    Discord normally rejects pathlike filenames at upload, but the zip is
    extracted by the requester's tooling — a crafted ``../../etc/passwd``
    that slipped past Discord's checks would be a Zip Slip vector.
    Belt-and-braces sanitisation costs ~nothing.

    ``PureWindowsPath`` is used regardless of host because it treats both
    ``/`` and ``\\`` as separators; ``PurePosixPath`` would let backslash
    paths through on Linux runners.
    """
    base = PureWindowsPath(filename).name
    if base in ("", ".", ".."):
        return "unnamed"
    return base


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


class ZipStreamResult:
    """The async-iterable zip stream paired with its counters.

    The ``counters`` property raises until the iterable has been drained
    to natural completion. Reading counts mid-stream would be a bug
    (the values are mutated incrementally as members are yielded), so
    the guard turns that into a loud failure rather than a silent
    wrong-answer. Mid-stream exceptions leave ``_drained=False``, which
    is the correct signal that the totals are not trustworthy.
    """

    def __init__(self, iterable: AsyncIterable[bytes], counters: Counters) -> None:
        self._counters = counters
        self._drained = False
        self.iterable = self._wrap(iterable)

    async def _wrap(self, inner: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
        async for chunk in inner:
            yield chunk
        self._drained = True

    @property
    def counters(self) -> Counters:
        if not self._drained:
            raise RuntimeError(
                "ZipStreamResult.counters read before the iterable was "
                "drained — counters are populated lazily during consumption "
                "and are only safe to read after the consumer (e.g. "
                "upload_blob) has fully iterated the stream."
            )
        return self._counters


async def _iter_chunks(
    resp: aiohttp.ClientResponse,
    filename: str,
    chunk_size: int,
) -> AsyncIterator[bytes]:
    """Yield chunks from an already-open response.

    The response lifecycle is owned by the ``async with`` in :func:`_members`
    — this generator only produces bytes. Any error during the body read
    corrupts the zip, so it surfaces as :class:`AttachmentStreamError`
    (caller aborts the whole job).
    """
    try:
        async for chunk in resp.content.iter_chunked(chunk_size):
            yield chunk
    except aiohttp.ClientError as e:
        raise AttachmentStreamError(
            f"Stream of '{filename}' failed mid-flight: {e}"
        ) from e


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
    behind. The response is held open by an ``async with`` that spans the
    chunk drain, so cancellation between yield and consumer iteration
    releases the connection rather than leaking it.
    """
    async for message in channel.history(limit=None):
        for attachment in message.attachments:
            content_type = (
                (attachment.content_type or "").split(";", 1)[0].strip().lower()
            )
            if content_type not in allowed_types:
                continue

            try:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Skipping attachment '%s' — HTTP %s",
                            attachment.filename,
                            resp.status,
                        )
                        continue

                    if "image" in content_type:
                        counters.images += 1
                    elif "video" in content_type:
                        counters.videos += 1

                    yield (
                        f"{message.id}_{_safe_filename(attachment.filename)}",
                        message.created_at,
                        S_IFREG | 0o600,
                        ZIP_64,
                        _iter_chunks(resp, attachment.filename, chunk_size),
                    )
            except aiohttp.ClientError as e:
                logger.warning(
                    "Skipping attachment '%s' — setup error: %s",
                    attachment.filename,
                    e,
                )
                continue


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
