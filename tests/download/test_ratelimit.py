"""Tests for the per-guild token-bucket rate limiter.

Uses ``fakeredis.aioredis`` so the WATCH/MULTI/EXEC path is exercised
against a real Redis-protocol implementation, with no compose stack
needed. ``time.time`` is monkeypatched where refill behaviour matters,
so the suite is deterministic.
"""

import fakeredis.aioredis
import pytest

from downloader_bot.download import ratelimit


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


class TestColdStart:
    async def test_first_acquire_is_allowed(self, redis):
        allowed, retry_after = await ratelimit.acquire(
            redis,
            guild_id=1,
            capacity=2,
            refill_per_hour=5,
        )

        assert allowed is True
        assert retry_after == 0.0

    async def test_first_acquire_creates_bucket_with_capacity_minus_one(self, redis):
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)

        # After one acquire from a fresh bucket: 2 - 1 = 1 token left.
        tokens = float(await redis.hget("ratelimit:guild:1", "tokens"))
        assert tokens == pytest.approx(1.0)


class TestBurstExhaustion:
    async def test_burst_capacity_is_respected(self, redis):
        # Two acquires within the same instant — both should succeed,
        # third should fail (capacity = 2).
        a1 = await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        a2 = await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        a3 = await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)

        assert a1[0] is True
        assert a2[0] is True
        assert a3[0] is False

    async def test_retry_after_is_positive_after_exhaustion(self, redis):
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)

        allowed, retry_after = await ratelimit.acquire(
            redis, guild_id=1, capacity=2, refill_per_hour=5
        )

        assert allowed is False
        # At 5/hour refill rate, one token = 720s. The exhausted bucket
        # needs the full 720s to recover one whole token.
        assert retry_after == pytest.approx(720.0, rel=0.01)


class TestRefill:
    async def test_token_refills_over_time(self, redis, monkeypatch):
        clock = [1_000_000.0]
        monkeypatch.setattr(ratelimit, "time", lambda: clock[0])

        # Exhaust the bucket.
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        denied, _ = await ratelimit.acquire(
            redis, guild_id=1, capacity=2, refill_per_hour=5
        )
        assert denied is False

        # Advance clock by exactly one refill period (720s = 1 token at 5/hr).
        clock[0] += 720.0

        allowed, _ = await ratelimit.acquire(
            redis, guild_id=1, capacity=2, refill_per_hour=5
        )
        assert allowed is True

    async def test_refill_is_capped_at_capacity(self, redis, monkeypatch):
        clock = [1_000_000.0]
        monkeypatch.setattr(ratelimit, "time", lambda: clock[0])

        # Drain to zero, then wait far longer than full refill.
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        # Capacity = 2 -> full refill = 2 * 720s = 1440s. Wait 10x that.
        clock[0] += 14_400.0

        # Bucket should be capped at 2 — three acquires in a row, third
        # must fail.
        a1 = await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        a2 = await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        a3 = await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)

        assert (a1[0], a2[0], a3[0]) == (True, True, False)


class TestKeyIsolation:
    async def test_each_guild_has_its_own_bucket(self, redis):
        # Exhaust guild 1.
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        denied, _ = await ratelimit.acquire(
            redis, guild_id=1, capacity=2, refill_per_hour=5
        )
        assert denied is False

        # Guild 2 is untouched — full burst still available.
        allowed, _ = await ratelimit.acquire(
            redis, guild_id=2, capacity=2, refill_per_hour=5
        )
        assert allowed is True


class TestTTL:
    async def test_bucket_has_ttl(self, redis):
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)

        ttl = await redis.ttl("ratelimit:guild:1")
        # _ttl_seconds(2, 5) = max(3600, 1440 * 2) = 3600.
        assert 0 < ttl <= 3600


class TestRefund:
    async def test_acquire_refund_round_trips_to_full_capacity(self, redis):
        # Acquire once (capacity 2 → 1 left), then refund → back to 2.
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)

        await ratelimit.refund(redis, guild_id=1, capacity=2, refill_per_hour=5)

        tokens = float(await redis.hget("ratelimit:guild:1", "tokens"))
        assert tokens == pytest.approx(2.0)

    async def test_refund_lets_exhausted_bucket_acquire_again(self, redis):
        # Two acquires drain capacity=2 to 0; third would be denied.
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        denied, _ = await ratelimit.acquire(
            redis, guild_id=1, capacity=2, refill_per_hour=5
        )
        assert denied is False

        # One refund → next acquire should succeed.
        await ratelimit.refund(redis, guild_id=1, capacity=2, refill_per_hour=5)

        allowed, _ = await ratelimit.acquire(
            redis, guild_id=1, capacity=2, refill_per_hour=5
        )
        assert allowed is True

    async def test_refund_of_absent_bucket_is_noop(self, redis):
        # No bucket key yet — TTL evicted, user has fully recovered.
        # Refund must not write a fresh row (that would let a refund
        # silently bump a brand-new user above capacity).
        await ratelimit.refund(redis, guild_id=999, capacity=2, refill_per_hour=5)

        assert await redis.exists("ratelimit:guild:999") == 0

    async def test_refund_at_capacity_is_noop(self, redis):
        # Acquire then refund leaves tokens=2; a second refund must NOT
        # push beyond capacity.
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        await ratelimit.refund(redis, guild_id=1, capacity=2, refill_per_hour=5)

        await ratelimit.refund(redis, guild_id=1, capacity=2, refill_per_hour=5)

        tokens = float(await redis.hget("ratelimit:guild:1", "tokens"))
        assert tokens == pytest.approx(2.0)

    async def test_refund_refreshes_ttl(self, redis):
        await ratelimit.acquire(redis, guild_id=1, capacity=2, refill_per_hour=5)
        # Drop TTL to a tiny value so the refund must reset it.
        await redis.expire("ratelimit:guild:1", 5)
        assert await redis.ttl("ratelimit:guild:1") <= 5

        await ratelimit.refund(redis, guild_id=1, capacity=2, refill_per_hour=5)

        ttl = await redis.ttl("ratelimit:guild:1")
        # Same _ttl_seconds(2, 5) = 3600 ceiling.
        assert ttl > 5
