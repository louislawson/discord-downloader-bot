"""Tests for the Redis-backed idempotency helpers.

These keys are how Taskiq retries skip already-completed phases. The
TTL applied here must match the SAS URL retention so a retry past the
TTL re-uploads (the cached URL would be expired anyway).

Uses ``fakeredis.aioredis`` so tests run without a real Redis instance.
"""

import fakeredis.aioredis
import pytest

from downloader_bot.download import idempotency


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


class TestArchiveUrlCache:
    async def test_returns_none_when_no_cached_url(self, redis):
        assert await idempotency.get_cached_archive_url(redis, "task-fresh") is None

    async def test_round_trip(self, redis):
        await idempotency.cache_archive_url(redis, "task-1", "https://example/x", 60)

        assert (
            await idempotency.get_cached_archive_url(redis, "task-1")
            == "https://example/x"
        )

    async def test_ttl_applied(self, redis):
        await idempotency.cache_archive_url(redis, "task-2", "https://x", 60)

        ttl = await redis.ttl("task:task-2:archive_url")
        assert 0 < ttl <= 60

    async def test_overwrite_resets_ttl_and_value(self, redis):
        # Second cache call replaces the first — used in the "retry
        # re-uploaded" path where the cache write failed last time.
        await idempotency.cache_archive_url(redis, "task-3", "https://old", 10)
        await idempotency.cache_archive_url(redis, "task-3", "https://new", 300)

        assert (
            await idempotency.get_cached_archive_url(redis, "task-3") == "https://new"
        )
        assert await redis.ttl("task:task-3:archive_url") > 60


class TestDeliveredMarker:
    async def test_is_delivered_false_when_no_marker(self, redis):
        assert await idempotency.is_delivered(redis, "task-undelivered") is False

    async def test_round_trip(self, redis):
        assert await idempotency.is_delivered(redis, "task-4") is False

        await idempotency.mark_delivered(redis, "task-4", 60)

        assert await idempotency.is_delivered(redis, "task-4") is True

    async def test_ttl_applied(self, redis):
        await idempotency.mark_delivered(redis, "task-5", 60)

        ttl = await redis.ttl("task:task-5:delivered")
        assert 0 < ttl <= 60


class TestKeyIsolation:
    """Each task_id has its own keyspace — no cross-contamination."""

    async def test_archive_url_keys_dont_collide(self, redis):
        await idempotency.cache_archive_url(redis, "task-a", "url-a", 60)
        await idempotency.cache_archive_url(redis, "task-b", "url-b", 60)

        assert await idempotency.get_cached_archive_url(redis, "task-a") == "url-a"
        assert await idempotency.get_cached_archive_url(redis, "task-b") == "url-b"

    async def test_delivered_marker_does_not_satisfy_archive_url_check(self, redis):
        # The two keys live at different paths; setting one mustn't make
        # the other appear set.
        await idempotency.mark_delivered(redis, "task-c", 60)

        assert await idempotency.get_cached_archive_url(redis, "task-c") is None

    async def test_archive_url_does_not_satisfy_delivered_check(self, redis):
        await idempotency.cache_archive_url(redis, "task-d", "url-d", 60)

        assert await idempotency.is_delivered(redis, "task-d") is False
