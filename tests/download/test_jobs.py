"""Tests for the user→jobs Redis index.

Uses ``fakeredis.aioredis`` so the pipeline(transaction=True) path is
exercised against a real Redis-protocol implementation — same shape as
``tests/download/test_ratelimit.py``.
"""

import fakeredis.aioredis
import pytest

from downloader_bot.download import jobs
from downloader_bot.download.jobs import JobMeta


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _meta(
    *,
    owner_user_id: int = 42,
    guild_id: int | None = 12345,
    channel_id: int = 555,
    enqueued_at_iso: str = "2026-05-18T12:00:00+00:00",
    enqueue_consumed_token: bool = True,
) -> JobMeta:
    return JobMeta(
        owner_user_id=owner_user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        enqueued_at_iso=enqueued_at_iso,
        enqueue_consumed_token=enqueue_consumed_token,
    )


class TestRecordAndGet:
    async def test_record_then_get_round_trips(self, redis):
        await jobs.record_job(
            redis,
            "task-1",
            meta=_meta(),
            ttl_seconds=3600,
        )

        result = await jobs.get_job_meta(redis, "task-1")

        assert result == _meta()

    async def test_get_missing_returns_none(self, redis):
        assert await jobs.get_job_meta(redis, "no-such-task") is None

    async def test_record_sets_ttl_on_meta_key(self, redis):
        await jobs.record_job(
            redis,
            "task-1",
            meta=_meta(),
            ttl_seconds=600,
        )

        ttl = await redis.ttl("task:task-1:meta")

        assert 0 < ttl <= 600

    async def test_record_sets_ttl_on_user_jobs_key(self, redis):
        # The atomic pipeline must EXPIRE the ZSET too — otherwise a
        # connection drop between ZADD and EXPIRE would leak the key
        # per affected user.
        await jobs.record_job(
            redis,
            "task-1",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )

        ttl = await redis.ttl("user:42:active_jobs")

        assert 0 < ttl <= 600


class TestLatestJobForUser:
    async def test_returns_none_for_user_with_no_jobs(self, redis):
        assert await jobs.latest_job_for_user(redis, user_id=42) is None

    async def test_returns_only_job_when_single(self, redis):
        await jobs.record_job(
            redis,
            "task-only",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )

        latest = await jobs.latest_job_for_user(redis, user_id=42)

        assert latest == "task-only"

    async def test_returns_most_recently_recorded_when_multiple(self, redis):
        # ZSET score = unix ts at record time → ZREVRANGE 0 0 picks latest.
        await jobs.record_job(
            redis,
            "task-old",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )
        await jobs.record_job(
            redis,
            "task-new",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )

        latest = await jobs.latest_job_for_user(redis, user_id=42)

        assert latest == "task-new"

    async def test_isolates_users(self, redis):
        await jobs.record_job(
            redis,
            "task-alice",
            meta=_meta(owner_user_id=1),
            ttl_seconds=600,
        )
        await jobs.record_job(
            redis,
            "task-bob",
            meta=_meta(owner_user_id=2),
            ttl_seconds=600,
        )

        assert await jobs.latest_job_for_user(redis, user_id=1) == "task-alice"
        assert await jobs.latest_job_for_user(redis, user_id=2) == "task-bob"


class TestForgetJob:
    async def test_drops_meta_and_zset_entry(self, redis):
        await jobs.record_job(
            redis,
            "task-1",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )

        await jobs.forget_job(redis, "task-1", owner_user_id=42)

        assert await jobs.get_job_meta(redis, "task-1") is None
        assert await jobs.latest_job_for_user(redis, user_id=42) is None

    async def test_forget_missing_is_idempotent(self, redis):
        # Should not raise even if neither key exists — covers the
        # "task crashed before record_job committed" edge.
        await jobs.forget_job(redis, "no-such-task", owner_user_id=42)

    async def test_forget_one_of_many_keeps_the_rest(self, redis):
        await jobs.record_job(
            redis,
            "task-1",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )
        await jobs.record_job(
            redis,
            "task-2",
            meta=_meta(owner_user_id=42),
            ttl_seconds=600,
        )

        await jobs.forget_job(redis, "task-1", owner_user_id=42)

        assert await jobs.get_job_meta(redis, "task-1") is None
        assert await jobs.get_job_meta(redis, "task-2") is not None
        assert await jobs.latest_job_for_user(redis, user_id=42) == "task-2"


class TestMetaShape:
    async def test_owner_token_flag_round_trips_true_and_false(self, redis):
        # The cancel path branches on enqueue_consumed_token; round-trip
        # both values so a future serializer change can't silently flip
        # the default.
        await jobs.record_job(
            redis,
            "task-consumed",
            meta=_meta(enqueue_consumed_token=True),
            ttl_seconds=600,
        )
        await jobs.record_job(
            redis,
            "task-bypass",
            meta=_meta(enqueue_consumed_token=False),
            ttl_seconds=600,
        )

        consumed = await jobs.get_job_meta(redis, "task-consumed")
        bypass = await jobs.get_job_meta(redis, "task-bypass")

        assert consumed["enqueue_consumed_token"] is True
        assert bypass["enqueue_consumed_token"] is False

    async def test_dm_context_guild_id_is_none(self, redis):
        # DM-context enqueues have guild_id=None; the cancel path uses
        # this to skip the refund (no bucket).
        await jobs.record_job(
            redis,
            "task-dm",
            meta=_meta(guild_id=None),
            ttl_seconds=600,
        )

        result = await jobs.get_job_meta(redis, "task-dm")

        assert result["guild_id"] is None
