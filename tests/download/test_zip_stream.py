"""Tests for the streaming-zip pipeline.

The tests verify three things:

1. ``_stream_response`` — chunked yielding and mid-stream error translation
   to ``AttachmentStreamError``. The response lifecycle is owned by
   ``_members`` (the response is pre-flighted, then ``release()`` is
   called in the chunks generator's ``finally``).
2. ``_members`` — pre-flight skipping (no zip entry left behind on
   setup-time failures), correct member-tuple construction, and that the
   per-attachment ``matches`` callable filters *before* the HTTP request
   so a non-matching attachment incurs zero network cost. The MIME
   normalisation that used to live here now lives in
   ``downloader_bot.download.filters`` and is covered there.
3. ``build_zip_stream`` end-to-end — round-trip producing a parseable
   zip including a unicode filename, plus a regression guard against
   accidental in-memory buffering of the full archive. Also asserts the
   ``before`` / ``after`` kwargs are forwarded to ``channel.history``.
"""

from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from zipfile import ZipFile

import aiohttp
import pytest

from downloader_bot.download.zip_stream import (
    AttachmentStreamError,
    NoMatchingAttachments,
    _compute_history_fraction,
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


def _mime_matcher(*allowed: str):
    """Test-only matcher: accept only attachments whose raw ``content_type`` matches."""
    allowed_set = set(allowed)
    return lambda att, _msg: att.content_type in allowed_set


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
            _mime_matcher("image/png"),
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
            m
            async for m in _members(
                session, channel, _mime_matcher("image/png"), chunk_size=64
            )
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
            m
            async for m in _members(
                session, channel, _mime_matcher("image/png"), chunk_size=64
            )
        ]

        assert members == []

    async def test_non_matching_attachment_does_not_request(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # The matcher rejects this attachment, so no HTTP call must happen.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("should not be called"))
        att = make_attachment(content_type="text/plain")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        members = [
            m
            async for m in _members(
                session, channel, _mime_matcher("image/png"), chunk_size=64
            )
        ]

        assert members == []

    async def test_matches_none_accepts_everything(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # matches=None means "no filtering" — every attachment goes through.
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

    async def test_forwards_before_and_after_to_channel_history(
        self,
        async_iter,
    ):
        # The history walk must be bounded by the resolved date window so
        # Discord does the date pruning for us instead of us walking the
        # entire channel and filtering client-side.
        session = MagicMock()
        channel = _channel_with([], async_iter)
        before = datetime(2026, 5, 13, tzinfo=UTC)
        after = datetime(2026, 5, 1, tzinfo=UTC)

        _ = [
            m
            async for m in _members(
                session,
                channel,
                None,
                chunk_size=64,
                before=before,
                after=after,
            )
        ]

        channel.history.assert_called_once_with(limit=None, before=before, after=after)

    async def test_omits_before_after_when_unset(self, async_iter):
        # When no date bounds are provided, channel.history must receive
        # only `limit=None` — passing `before=None` / `after=None`
        # explicitly would change discord.py's behaviour vs omission.
        session = MagicMock()
        channel = _channel_with([], async_iter)

        _ = [m async for m in _members(session, channel, None, chunk_size=64)]

        channel.history.assert_called_once_with(limit=None)


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
            matches=_mime_matcher("image/png"),
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
            matches=_mime_matcher("image/png"),
            chunk_size=chunk_size,
        )
        _buf, sizes = await _drain_to_buffer(stream)

        # Generous threshold: stream-zip's default output chunk_size is 64 KB,
        # so each yielded chunk should be well under 1 MB. A regression to
        # whole-archive materialisation would produce a single ~2 MB chunk.
        assert max(sizes) < 1024 * 1024, (
            f"peak yield was {max(sizes)} bytes — pipeline may be buffering"
        )

    async def test_empty_channel_raises_no_matching_attachments(self, async_iter):
        # No messages → peek wrapper raises before any bytes hit the consumer,
        # so the worker can short-circuit instead of uploading an empty zip.
        session = MagicMock()
        channel = _channel_with([], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            matches=None,
            chunk_size=64,
        )

        with pytest.raises(NoMatchingAttachments):
            await _drain_to_buffer(stream)

    async def test_all_attachments_filtered_out_raises(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Every attachment is text/plain but the matcher accepts only
        # image/png. _members yields nothing → peek wrapper raises.
        session = MagicMock()
        att = make_attachment(content_type="text/plain", filename="readme.txt")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            matches=_mime_matcher("image/png"),
            chunk_size=64,
        )

        with pytest.raises(NoMatchingAttachments):
            await _drain_to_buffer(stream)

    async def test_all_pre_flight_failures_raises(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Every attachment GET returns 404; _members skips them all and
        # exhausts. The peek wrapper raises so a useless empty zip is
        # never uploaded.
        resp = _make_response(status=404)
        session = _make_session(resp)
        att = make_attachment(content_type="image/png", filename="missing.png")
        msg = make_message(attachments=(att,))
        channel = _channel_with([msg], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            matches=_mime_matcher("image/png"),
            chunk_size=64,
        )

        with pytest.raises(NoMatchingAttachments):
            await _drain_to_buffer(stream)

    async def test_peek_does_not_swallow_first_member(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Single matching attachment must round-trip through the peek
        # wrapper unmodified (regression guard: the peek read the first
        # member and could drop it if not re-yielded).
        body = b"only"
        resp = _make_response(chunks=(body,))
        session = _make_session(resp)
        att = make_attachment(content_type="image/png", filename="solo.png")
        msg = make_message(message_id=11, attachments=(att,))
        channel = _channel_with([msg], async_iter)

        stream = build_zip_stream(
            session,
            channel,
            matches=_mime_matcher("image/png"),
            chunk_size=64,
        )
        buf, _sizes = await _drain_to_buffer(stream)

        with ZipFile(buf) as zf:
            assert zf.namelist() == ["11_solo.png"]
            assert zf.read("11_solo.png") == body

    async def test_before_after_kwargs_forward_to_history(self, async_iter):
        # build_zip_stream is the public entry; ensure date bounds make
        # it through into the channel.history call.
        session = MagicMock()
        channel = _channel_with([], async_iter)
        before = datetime(2026, 5, 10, tzinfo=UTC)
        after = datetime(2026, 5, 1, tzinfo=UTC)

        stream = build_zip_stream(
            session,
            channel,
            before=before,
            after=after,
            chunk_size=64,
        )
        with pytest.raises(NoMatchingAttachments):
            await _drain_to_buffer(stream)

        # The history walk now receives two extra arguments alongside
        # before/after: a sentinel call from _resolve_snowflake_bounds
        # may not fire (after is set), but the main walk must still
        # carry limit=None + the date bounds.
        main_call = channel.history.call_args_list[-1]
        assert main_call.kwargs == {"limit": None, "before": before, "after": after}


# --- history-fraction math ------------------------------------------------


class TestComputeHistoryFraction:
    def test_at_high_end_returns_zero(self):
        # First message of the walk is the newest → snowflake ~= high.
        assert _compute_history_fraction(current_id=100, low=0, high=100) == 0.0

    def test_at_low_end_returns_one(self):
        # Last message of the walk is the oldest → snowflake ~= low.
        assert _compute_history_fraction(current_id=0, low=0, high=100) == 1.0

    def test_midpoint_returns_half(self):
        assert _compute_history_fraction(current_id=50, low=0, high=100) == 0.5

    def test_high_equals_low_returns_one(self):
        # Single-message channel — denominator zero, defaults to "done."
        assert _compute_history_fraction(current_id=42, low=42, high=42) == 1.0

    def test_current_outside_range_clamps(self):
        # current_id past the high end (or before low) shouldn't produce
        # negative or >1 values — the embed says "~37%" so out-of-range
        # would render as "~-15%" which is nonsense.
        assert _compute_history_fraction(current_id=200, low=0, high=100) == 0.0
        assert _compute_history_fraction(current_id=-50, low=0, high=100) == 1.0


# --- on_progress callback ------------------------------------------------


class TestOnProgress:
    async def test_fires_per_message_scanned_with_throttle_zero(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Throttle=0 forces a tick on every message — used to assert the
        # callback fires per *message* (not per attachment), so filter-
        # stall stretches still tick the heartbeat.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("filter rejects"))
        att = make_attachment(content_type="text/plain")
        msgs = [make_message(message_id=i, attachments=(att,)) for i in (3, 2, 1)]
        channel = _channel_with(msgs, async_iter)
        # Real channels return Optional[int]; the bare MagicMock default
        # is incompatible with the snowflake-fraction math.
        channel.last_message_id = 10
        # Pass after= so the bounds resolver doesn't fetch oldest_first
        # (which would consume the shared history iterator before the
        # main walk gets to it).
        after = datetime(2025, 1, 1, tzinfo=UTC)

        snapshots = []

        async def on_progress(snapshot):
            snapshots.append(snapshot)

        members = [
            m
            async for m in _members(
                session,
                channel,
                _mime_matcher("image/png"),  # rejects every attachment
                chunk_size=64,
                after=after,
                on_progress=on_progress,
                progress_throttle_seconds=0.0,
            )
        ]

        assert members == []  # nothing matched, but heartbeat still ticked
        assert len(snapshots) == 3
        # attachments_done stays at 0 across the whole walk
        assert all(s["attachments_done"] == 0 for s in snapshots)
        assert all(s["bytes_streamed"] == 0 for s in snapshots)

    async def test_throttle_suppresses_redundant_emits(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Throttle=999s means only the very first tick (no preceding
        # tick) won't fire — and even that one requires monotonic to
        # advance. Without time advancement, no ticks should fire after
        # the first scan.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("filter rejects"))
        att = make_attachment(content_type="text/plain")
        msgs = [make_message(message_id=i, attachments=(att,)) for i in (3, 2, 1)]
        channel = _channel_with(msgs, async_iter)

        snapshots = []

        async def on_progress(snapshot):
            snapshots.append(snapshot)

        [
            m
            async for m in _members(
                session,
                channel,
                _mime_matcher("image/png"),
                chunk_size=64,
                on_progress=on_progress,
                progress_throttle_seconds=999.0,
            )
        ]

        # last_emit is initialised to monotonic() at function entry, so
        # no scan within ~999s clears the threshold. Zero emits.
        assert snapshots == []

    async def test_attachments_done_and_bytes_streamed_increment(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Real matches → attachments_done and bytes_streamed should
        # advance as each member tuple is yielded.
        resp_a = _make_response(chunks=(b"a",))
        resp_b = _make_response(chunks=(b"b",))
        session = _make_session(resp_a, resp_b)

        att_a = make_attachment(filename="a.png")
        att_a.size = 100
        att_b = make_attachment(filename="b.png")
        att_b.size = 200
        msg_a = make_message(message_id=3, attachments=(att_a,))
        msg_b = make_message(message_id=2, attachments=(att_b,))
        channel = _channel_with([msg_a, msg_b], async_iter)
        channel.last_message_id = 10
        after = datetime(2025, 1, 1, tzinfo=UTC)

        snapshots = []

        async def on_progress(snapshot):
            snapshots.append(dict(snapshot))

        members = []
        async for member in _members(
            session,
            channel,
            None,  # accept everything
            chunk_size=64,
            after=after,
            on_progress=on_progress,
            progress_throttle_seconds=0.0,
        ):
            # Drain so the inner stream completes and bytes are accounted.
            async for _ in member[4]:
                pass
            members.append(member)

        assert len(members) == 2
        # By the second tick (start of second message), the first
        # attachment has been yielded → attachments_done == 1, bytes == 100.
        # Snapshot ordering: tick fires *before* the inner loop, so:
        #   tick 1 (msg_a start): done=0, bytes=0
        #   tick 2 (msg_b start): done=1, bytes=100
        assert snapshots[0]["attachments_done"] == 0
        assert snapshots[0]["bytes_streamed"] == 0
        assert snapshots[1]["attachments_done"] == 1
        assert snapshots[1]["bytes_streamed"] == 100

    async def test_callback_error_does_not_kill_walk(
        self,
        async_iter,
        make_attachment,
        make_message,
        caplog,
    ):
        # Progress is best-effort — a Redis blip in on_progress shouldn't
        # abort an in-flight zip job.
        resp = _make_response(chunks=(b"x",))
        session = _make_session(resp)
        att = make_attachment(filename="a.png")
        att.size = 50
        msg = make_message(message_id=1, attachments=(att,))
        channel = _channel_with([msg], async_iter)
        channel.last_message_id = 10
        after = datetime(2025, 1, 1, tzinfo=UTC)

        async def on_progress(_snapshot):
            raise RuntimeError("redis down")

        members = []
        async for member in _members(
            session,
            channel,
            None,
            chunk_size=64,
            after=after,
            on_progress=on_progress,
            progress_throttle_seconds=0.0,
        ):
            async for _ in member[4]:
                pass
            members.append(member)

        # The walk completes even though the callback raised.
        assert len(members) == 1

    async def test_no_on_progress_means_no_extra_history_call(self, async_iter):
        # The snowflake-bounds resolver only fires when on_progress is
        # provided. Without it, channel.history should be called once
        # (the main walk) — never twice (snowflake-bounds + main).
        session = MagicMock()
        channel = _channel_with([], async_iter)

        _ = [
            m
            async for m in _members(
                session,
                channel,
                None,
                chunk_size=64,
            )
        ]

        assert channel.history.call_count == 1


# --- snowflake bounds + filter-stall integration -------------------------


class TestSnowflakeBounds:
    async def test_history_fraction_advances_through_filter_stall(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # Bounded scan via after= → low snowflake comes from the after
        # arg (no extra history call needed). Filter rejects every
        # attachment, but history_fraction should still climb from
        # ~0.0 toward ~1.0 across ticks.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("filter rejects"))
        att = make_attachment(content_type="text/plain")
        # Snowflakes monotonically decrease as we walk newer→older;
        # use plausible spread so the fraction math has resolution.
        msgs = [
            make_message(message_id=mid, attachments=(att,))
            for mid in (900_000_000_000, 500_000_000_000, 100_000_000_000)
        ]
        channel = _channel_with(msgs, async_iter)
        # last_message_id provides the high snowflake when before is
        # None; we set both bounds via after= so no extra API call fires.
        channel.last_message_id = 1_000_000_000_000

        after = datetime(2025, 1, 1, tzinfo=UTC)

        snapshots = []

        async def on_progress(snapshot):
            snapshots.append(dict(snapshot))

        [
            m
            async for m in _members(
                session,
                channel,
                _mime_matcher("image/png"),
                chunk_size=64,
                after=after,
                on_progress=on_progress,
                progress_throttle_seconds=0.0,
            )
        ]

        assert len(snapshots) == 3
        # attachments_done stays at 0 (filter stall), but the heartbeat
        # advanced through the walk.
        assert [s["attachments_done"] for s in snapshots] == [0, 0, 0]
        # history_fraction should be monotonically non-decreasing as we
        # walk older messages.
        fractions = [s["history_fraction"] for s in snapshots]
        assert fractions == sorted(fractions)

    async def test_fully_unbounded_scan_fetches_oldest_message(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # No before, no after → _resolve_snowflake_bounds makes one extra
        # channel.history(limit=1, oldest_first=True) call to find low.
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("filter rejects"))
        att = make_attachment(content_type="text/plain")
        walk_msgs = [
            make_message(message_id=mid, attachments=(att,))
            for mid in (900_000_000_000, 500_000_000_000)
        ]
        oldest_msg = make_message(message_id=100_000_000_000)

        # channel.history is called twice: once for the bounds lookup
        # (limit=1, oldest_first=True) then once for the actual walk.
        call_count = {"n": 0}

        def history(*args, **kwargs):
            call_count["n"] += 1
            if kwargs.get("oldest_first"):
                return async_iter([oldest_msg])
            return async_iter(walk_msgs)

        channel = MagicMock()
        channel.history = MagicMock(side_effect=history)
        channel.last_message_id = 1_000_000_000_000

        async def on_progress(_snapshot):
            pass

        [
            m
            async for m in _members(
                session,
                channel,
                _mime_matcher("image/png"),
                chunk_size=64,
                on_progress=on_progress,
                progress_throttle_seconds=0.0,
            )
        ]

        # Two history calls: one for the bounds lookup, one for the walk.
        assert call_count["n"] == 2
        bounds_call = channel.history.call_args_list[0]
        assert bounds_call.kwargs.get("oldest_first") is True
        assert bounds_call.kwargs.get("limit") == 1

    async def test_history_fraction_is_none_when_bounds_unresolved(
        self,
        async_iter,
        make_attachment,
        make_message,
    ):
        # No after= and the oldest-message lookup raises → we can't
        # compute a denominator. ProgressSnapshot.history_fraction must
        # be None so the embed skips the "Position" field rather than
        # lying about being "~0% through channel history."
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("filter rejects"))
        att = make_attachment(content_type="text/plain")
        walk_msgs = [
            make_message(message_id=mid, attachments=(att,))
            for mid in (900_000_000_000, 500_000_000_000)
        ]

        def history(*args, **kwargs):
            if kwargs.get("oldest_first"):
                raise RuntimeError("CDN down")
            return async_iter(walk_msgs)

        channel = MagicMock()
        channel.history = MagicMock(side_effect=history)
        channel.last_message_id = 1_000_000_000_000

        snapshots = []

        async def on_progress(snapshot):
            snapshots.append(dict(snapshot))

        [
            m
            async for m in _members(
                session,
                channel,
                _mime_matcher("image/png"),
                chunk_size=64,
                on_progress=on_progress,
                progress_throttle_seconds=0.0,
            )
        ]

        assert len(snapshots) == 2
        assert all(s["history_fraction"] is None for s in snapshots)
