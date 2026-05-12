"""Tests for the streaming-zip pipeline.

The tests verify three things:

1. ``_stream_response`` — chunked yielding and mid-stream error translation
   to ``AttachmentStreamError``. The response lifecycle is owned by
   ``_members`` (the response is pre-flighted, then ``release()`` is
   called in the chunks generator's ``finally``).
2. ``_members`` — pre-flight skipping (no zip entry left behind on
   setup-time failures), correct member-tuple construction, and that
   ``allowed_types`` filtering happens *before* the HTTP request so a
   disallowed content type incurs zero network cost.
3. ``build_zip_stream`` end-to-end — round-trip producing a parseable
   zip including a unicode filename, plus a regression guard against
   accidental in-memory buffering of the full archive.
"""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from zipfile import ZipFile

import aiohttp
import pytest

from downloader_bot.download.zip_stream import (
    AttachmentStreamError,
    _members,
    _stream_response,
    build_zip_stream,
)


def _make_response(*, status: int = 200, chunks: tuple[bytes, ...] = ()) -> MagicMock:
    """Mock ``aiohttp.ClientResponse`` with ``content.iter_chunked`` + ``release``."""
    resp = MagicMock()
    resp.status = status
    resp.release = MagicMock()

    async def _iter_chunked(_size: int):
        for chunk in chunks:
            yield chunk

    resp.content = MagicMock()
    resp.content.iter_chunked = _iter_chunked
    return resp


def _make_session(*responses) -> MagicMock:
    """Mock session whose ``get(url).__aenter__()`` returns sequential responses.

    Each call to ``session.get(url)`` returns an object whose ``__aenter__``
    resolves to the next response. The orchestrator code uses the unusual
    ``await session.get(url).__aenter__()`` pattern (so it can keep the
    response open across the streaming generator), which we mirror here.
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


def _channel_with(messages, async_iter):
    channel = MagicMock()
    channel.history.return_value = async_iter(messages)
    return channel


# --- _stream_response ------------------------------------------------------


class TestStreamResponse:
    async def test_yields_chunks(self):
        resp = _make_response(chunks=(b"aa", b"bb", b"cc"))

        out = [c async for c in _stream_response(resp, "x.png", chunk_size=2)]

        assert out == [b"aa", b"bb", b"cc"]
        # ``release`` runs in the ``finally`` once the generator exits.
        resp.release.assert_called_once()

    async def test_mid_stream_client_error_raises_attachment_stream_error(self):
        async def _broken_iter(_size: int):
            yield b"first"
            raise aiohttp.ClientError("connection reset")

        resp = MagicMock()
        resp.status = 200
        resp.release = MagicMock()
        resp.content = MagicMock()
        resp.content.iter_chunked = _broken_iter

        with pytest.raises(AttachmentStreamError, match="failed mid-flight"):
            async for _ in _stream_response(resp, "x.png", chunk_size=64):
                pass

        # ``release`` still runs on the failure path.
        resp.release.assert_called_once()


# --- _members --------------------------------------------------------------


class TestMembers:
    async def test_yields_tuple_for_200_response(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        resp = _make_response(chunks=(b"abc",))
        session = _make_session(resp)
        att = make_attachment(content_type="image/png", filename="a.png")
        msg = make_message(message_id=42, attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = []
        async for member in _members(
            session,
            channel,
            {"image/png"},
            chunk_size=64,
        ):
            # Drain the chunks generator inside the member tuple so the
            # response's release() fires before we move on.
            async for _ in member[4]:
                pass
            members.append(member)

        assert len(members) == 1
        name, mtime, _mode, _method, _chunks = members[0]
        assert name == "42_a.png"
        assert mtime == msg.created_at

    async def test_skips_non_200_response_with_no_member_tuple(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        resp = _make_response(status=404)
        session = _make_session(resp)
        att = make_attachment()
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = [
            m async for m in _members(session, channel, {"image/png"}, chunk_size=64)
        ]

        # No member tuple yielded → no zip entry created downstream.
        assert members == []
        # The skipped response was released (no connection leak).
        resp.release.assert_called_once()

    async def test_skips_setup_client_error_with_no_member_tuple(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        session = _make_session(aiohttp.ClientError("dns"))
        att = make_attachment()
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = [
            m async for m in _members(session, channel, {"image/png"}, chunk_size=64)
        ]

        assert members == []

    async def test_skips_disallowed_content_type_without_request(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # No HTTP call should happen for an attachment whose content type
        # isn't in the allowed set.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("should not be called"))
        att = make_attachment(content_type="text/plain")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = [
            m async for m in _members(session, channel, {"image/png"}, chunk_size=64)
        ]

        assert members == []

    async def test_allowed_types_none_accepts_everything(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # allowed_types=None means accept all content types.
        resp = _make_response(chunks=(b"x",))
        session = _make_session(resp)
        att = make_attachment(content_type="text/plain", filename="weird.txt")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = []
        async for member in _members(session, channel, None, chunk_size=64):
            async for _ in member[4]:
                pass
            members.append(member)

        assert len(members) == 1

    async def test_strips_content_type_parameters_before_match(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # "image/png; charset=utf-8" should match the {"image/png"} filter.
        resp = _make_response(chunks=(b"x",))
        session = _make_session(resp)
        att = make_attachment(content_type="image/png; charset=utf-8")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = []
        async for member in _members(
            session,
            channel,
            {"image/png"},
            chunk_size=64,
        ):
            async for _ in member[4]:
                pass
            members.append(member)

        assert len(members) == 1


# --- build_zip_stream end-to-end ------------------------------------------


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
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        body_a = b"hello"
        body_b = "héllo 🎉".encode()
        resp_a = _make_response(chunks=(body_a,))
        resp_b = _make_response(chunks=(body_b,))
        session = _make_session(resp_a, resp_b)

        att_a = make_attachment(filename="ascii.png")
        att_b = make_attachment(
            filename="naïve_😀.png",
            content_type="image/png",
            url="https://cdn.example/u.png",
        )
        msg = make_message(message_id=7, attachments=(att_a, att_b))
        channel = _channel_with([msg], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            allowed_types={"image/png"},
            chunk_size=64,
        )
        buf, _sizes = await _drain_to_buffer(stream)

        with ZipFile(buf) as zf:
            names = zf.namelist()
            assert "7_ascii.png" in names
            assert "7_naïve_😀.png" in names
            assert zf.read("7_ascii.png") == body_a
            assert zf.read("7_naïve_😀.png") == body_b

    async def test_streaming_pipeline_does_not_buffer_full_zip(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # 32 x 64 KB = 2 MB total. If a future change accidentally collapses
        # the pipeline to a single in-memory archive, we'd see one ~2 MB
        # yield from the iterable; streaming yields stay bounded by
        # stream-zip's internal chunk_size (~64 KB).
        chunk_size = 64 * 1024
        chunk_count = 32
        big_chunk = b"x" * chunk_size
        resp = _make_response(chunks=tuple(big_chunk for _ in range(chunk_count)))
        session = _make_session(resp)

        att = make_attachment(filename="big.bin", content_type="image/png")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            allowed_types={"image/png"},
            chunk_size=chunk_size,
        )
        _buf, sizes = await _drain_to_buffer(stream)

        # Generous threshold: stream-zip's default output chunk_size is 64 KB,
        # so each yielded chunk should be well under 1 MB. A regression to
        # whole-archive materialisation would produce a single ~2 MB chunk.
        assert max(sizes) < 1024 * 1024, (
            f"peak yield was {max(sizes)} bytes — pipeline may be buffering"
        )

    async def test_empty_channel_produces_valid_empty_zip(self, async_iter):
        # No messages → stream yields the central-directory-only archive.
        # zipfile should still read it as a valid (empty) zip.
        session = MagicMock()
        channel = _channel_with([], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            allowed_types=None,
            chunk_size=64,
        )
        buf, _sizes = await _drain_to_buffer(stream)

        with ZipFile(buf) as zf:
            assert zf.namelist() == []
