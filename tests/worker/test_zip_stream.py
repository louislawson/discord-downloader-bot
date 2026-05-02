"""Tests for the streaming-zip pipeline.

The tests verify three things:

1. ``_stream_response`` — chunked yielding, mid-stream error translation
   to ``AttachmentStreamError``, and connection release on every exit path.
2. ``_members`` — pre-flight skipping (no zip entry left behind on
   setup-time failures), counter accounting, and correct member-tuple
   construction.
3. ``build_zip_stream`` end-to-end — round-trip producing a parseable
   zip including a unicode filename, plus a regression guard against
   accidental in-memory buffering of the full archive.
"""

from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from zipfile import ZipFile

import aiohttp
import pytest

from downloader_bot.worker.zip_stream import (
    AttachmentStreamError,
    Counters,
    _members,
    _stream_response,
    build_zip_stream,
)


def _make_response(*, status: int = 200, chunks: tuple[bytes, ...] = ()) -> MagicMock:
    """Mock ``aiohttp.ClientResponse`` with ``content.iter_chunked`` and ``release``."""
    resp = MagicMock()
    resp.status = status

    async def _iter_chunked(_size: int):
        for chunk in chunks:
            yield chunk

    resp.content = MagicMock()
    resp.content.iter_chunked = _iter_chunked
    resp.release = MagicMock()
    return resp


def _make_session(*responses) -> MagicMock:
    """Mock session whose ``get(url)`` returns sequential context managers.

    Each context manager's ``__aenter__`` resolves to the next response, or
    raises if the response is an exception instance.
    """
    session = MagicMock()
    cms = []
    for resp in responses:
        cm = MagicMock()
        if isinstance(resp, BaseException):
            cm.__aenter__ = AsyncMock(side_effect=resp)
        else:
            cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        cms.append(cm)
    session.get = MagicMock(side_effect=cms)
    return session


def _make_attachment(
    *,
    url: str = "https://cdn.example/x.png",
    filename: str = "x.png",
    content_type: str = "image/png",
) -> MagicMock:
    att = MagicMock()
    att.url = url
    att.filename = filename
    att.content_type = content_type
    return att


def _make_message(
    *,
    message_id: int = 1,
    created_at: datetime | None = None,
    attachments: tuple = (),
) -> MagicMock:
    msg = MagicMock()
    msg.id = message_id
    msg.created_at = created_at or datetime(2026, 1, 1, tzinfo=UTC)
    msg.attachments = list(attachments)
    return msg


def _channel_with(messages, async_iter):
    channel = MagicMock()
    channel.history.return_value = async_iter(messages)
    return channel


# --- _stream_response -------------------------------------------------------


class TestStreamResponse:
    async def test_yields_chunks_then_releases(self):
        resp = _make_response(chunks=(b"aa", b"bb", b"cc"))

        out = [c async for c in _stream_response(resp, "x.png", chunk_size=2)]

        assert out == [b"aa", b"bb", b"cc"]
        resp.release.assert_called_once()

    async def test_mid_stream_client_error_raises_attachment_stream_error(self):
        async def _broken_iter(_size: int):
            yield b"first"
            raise aiohttp.ClientError("connection reset")

        resp = MagicMock()
        resp.status = 200
        resp.content = MagicMock()
        resp.content.iter_chunked = _broken_iter
        resp.release = MagicMock()

        with pytest.raises(AttachmentStreamError, match="failed mid-flight"):
            async for _ in _stream_response(resp, "x.png", chunk_size=64):
                pass

        # Connection must be released even when the body raises.
        resp.release.assert_called_once()


# --- _members ---------------------------------------------------------------


class TestMembers:
    async def test_yields_tuple_for_200_response_and_bumps_counters(self, async_iter):
        resp = _make_response(chunks=(b"abc",))
        session = _make_session(resp)
        att = _make_attachment(content_type="image/png", filename="a.png")
        msg = _make_message(message_id=42, attachments=(att,))
        channel = _channel_with([msg], async_iter)
        counters = Counters()

        members = []
        async for member in _members(
            session, channel, {"image/png"}, counters, chunk_size=64
        ):
            members.append(member)

        assert len(members) == 1
        name, mtime, _mode, _method, _chunks = members[0]
        assert name == "42_a.png"
        assert mtime == msg.created_at
        assert counters.images == 1
        assert counters.videos == 0

    async def test_skips_non_200_response_with_no_member_tuple(self, async_iter):
        resp = _make_response(status=404)
        session = _make_session(resp)
        att = _make_attachment()
        msg = _make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)
        counters = Counters()

        members = [
            m
            async for m in _members(
                session, channel, {"image/png"}, counters, chunk_size=64
            )
        ]

        # No member tuple yielded → no zip entry created downstream.
        assert members == []
        assert counters.images == 0
        # Skipped response must still be released.
        resp.release.assert_called_once()

    async def test_skips_setup_client_error_with_no_member_tuple(self, async_iter):
        session = _make_session(aiohttp.ClientError("dns"))
        att = _make_attachment()
        msg = _make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)
        counters = Counters()

        members = [
            m
            async for m in _members(
                session, channel, {"image/png"}, counters, chunk_size=64
            )
        ]

        assert members == []
        assert counters.images == 0

    async def test_skips_disallowed_content_type_without_request(self, async_iter):
        # No HTTP call should happen for an attachment whose content type
        # isn't in the allowed set.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("should not be called"))
        att = _make_attachment(content_type="text/plain")
        msg = _make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)
        counters = Counters()

        members = [
            m
            async for m in _members(
                session, channel, {"image/png"}, counters, chunk_size=64
            )
        ]

        assert members == []
        assert counters.images == 0

    async def test_video_content_type_increments_video_counter(self, async_iter):
        resp = _make_response(chunks=(b"v",))
        session = _make_session(resp)
        att = _make_attachment(content_type="video/mp4", filename="v.mp4")
        msg = _make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)
        counters = Counters()

        async for _ in _members(
            session, channel, {"video/mp4"}, counters, chunk_size=64
        ):
            pass

        assert counters.videos == 1
        assert counters.images == 0


# --- build_zip_stream end-to-end -------------------------------------------


async def _drain_to_buffer(iterable) -> tuple[BytesIO, list[int]]:
    """Drain an async byte iterable into a BytesIO; return buffer + per-chunk sizes."""
    buf = BytesIO()
    sizes: list[int] = []
    async for chunk in iterable:
        sizes.append(len(chunk))
        buf.write(chunk)
    buf.seek(0)
    return buf, sizes


class TestBuildZipStream:
    async def test_round_trip_produces_parseable_zip_with_unicode_filename(
        self, async_iter
    ):
        body_a = b"hello"
        body_b = "héllo 🎉".encode()
        resp_a = _make_response(chunks=(body_a,))
        resp_b = _make_response(chunks=(body_b,))
        session = _make_session(resp_a, resp_b)

        att_a = _make_attachment(filename="ascii.png")
        att_b = _make_attachment(
            filename="naïve_😀.png",
            content_type="image/png",
            url="https://cdn.example/u.png",
        )
        msg = _make_message(message_id=7, attachments=(att_a, att_b))
        channel = _channel_with([msg], async_iter)

        result = build_zip_stream(session, channel, {"image/png"}, chunk_size=64)
        buf, _sizes = await _drain_to_buffer(result.iterable)

        with ZipFile(buf) as zf:
            names = zf.namelist()
            assert "7_ascii.png" in names
            assert "7_naïve_😀.png" in names
            assert zf.read("7_ascii.png") == body_a
            assert zf.read("7_naïve_😀.png") == body_b

        assert result.counters.images == 2
        assert result.counters.videos == 0

    async def test_streaming_pipeline_does_not_buffer_full_zip(self, async_iter):
        # 32 x 64 KB = 2 MB total. If a future change accidentally collapses
        # the pipeline to a single in-memory archive, we'd see one ~2 MB
        # yield from the iterable; streaming yields stay bounded by
        # stream-zip's internal chunk_size (~64 KB).
        chunk_size = 64 * 1024
        chunk_count = 32
        big_chunk = b"x" * chunk_size
        resp = _make_response(chunks=tuple(big_chunk for _ in range(chunk_count)))
        session = _make_session(resp)

        att = _make_attachment(filename="big.bin", content_type="image/png")
        msg = _make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        result = build_zip_stream(
            session, channel, {"image/png"}, chunk_size=chunk_size
        )
        _buf, sizes = await _drain_to_buffer(result.iterable)

        # Generous threshold: stream-zip's default output chunk_size is 64 KB,
        # so each yielded chunk should be well under 1 MB. A regression to
        # whole-archive materialisation would produce a single ~2 MB chunk.
        assert max(sizes) < 1024 * 1024, (
            f"peak yield was {max(sizes)} bytes — pipeline may be buffering"
        )
