"""Redis-backed token bucket for per-guild ``/download`` rate limiting.

Each guild has a bucket at ``ratelimit:guild:{guild_id}`` with two fields:
``tokens`` (current balance, float) and ``last_refill`` (Unix timestamp,
float). Issuing a ``/download`` consumes one token; the bucket refills at
``refill_per_hour`` tokens/hour and is capped at ``capacity`` (the burst
allowance).

The acquire / refund ops use Redis ``WATCH`` / ``MULTI`` / ``EXEC`` so
concurrent writers (multiple bot instances, rapid retries, a cancel
landing in the same instant as a new acquire) can't desync a single
guild's bucket. State persists in Redis, so the limit survives bot
restarts — which is the whole point versus an in-memory cooldown.
"""

from time import time

from redis.asyncio import Redis
from redis.exceptions import WatchError

_KEY = "ratelimit:guild:{guild_id}"


def _ttl_seconds(capacity: int, refill_per_hour: int) -> int:
    """Bucket TTL: 2x the worst-case full-refill time, min 1 hour.

    A bucket that has been idle long enough to fully refill can be safely
    evicted; the next acquire just initialises a fresh full bucket, which
    is identical to the state Redis would have held anyway.

    Args:
        capacity: Maximum tokens the bucket can hold.
        refill_per_hour: Refill rate, in tokens per hour.

    Returns:
        TTL in seconds.
    """
    full_refill = capacity * 3600 // max(refill_per_hour, 1)
    return max(3600, full_refill * 2)


async def acquire(
    redis: Redis,
    guild_id: int,
    *,
    capacity: int,
    refill_per_hour: int,
) -> tuple[bool, float]:
    """Attempt to consume one token from the guild's bucket.

    Atomic under contention: optimistic-locks the bucket key with ``WATCH``
    and retries on ``WatchError`` if another client modified it between the
    read and the write.

    Args:
        redis: App-namespaced Redis client (must have ``decode_responses=True``).
        guild_id: Discord guild ID; namespaces the bucket key.
        capacity: Burst allowance — max tokens the bucket can hold.
        refill_per_hour: Long-run rate cap, tokens per hour.

    Returns:
        ``(True, 0.0)`` when a token was consumed.
        ``(False, retry_after_seconds)`` when the bucket was empty; the
        float is how long the caller must wait for one token to refill.
    """
    key = _KEY.format(guild_id=guild_id)
    refill_rate = refill_per_hour / 3600.0
    ttl = _ttl_seconds(capacity, refill_per_hour)

    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw_tokens, raw_last = await pipe.hmget(key, "tokens", "last_refill")
                now = time()

                if raw_tokens is None:
                    tokens = float(capacity)
                else:
                    elapsed = max(0.0, now - float(raw_last))
                    tokens = min(
                        float(capacity),
                        float(raw_tokens) + elapsed * refill_rate,
                    )

                if tokens >= 1.0:
                    tokens -= 1.0
                    allowed = True
                    retry_after = 0.0
                else:
                    allowed = False
                    retry_after = (1.0 - tokens) / refill_rate

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={"tokens": tokens, "last_refill": now},
                )
                pipe.expire(key, ttl)
                await pipe.execute()
                return allowed, retry_after
            except WatchError:
                # Another client modified the key between WATCH and EXEC.
                # Retry from scratch with a fresh read.
                continue


async def refund(
    redis: Redis,
    guild_id: int,
    *,
    capacity: int,
    refill_per_hour: int,
) -> None:
    """Return one token to the guild's bucket, capped at ``capacity``.

    Called when a queued ``/download`` is cancelled before the worker
    picks it up — the enqueue counted against the bucket but no work
    happened, so the user shouldn't be penalised on retry. The cog is
    responsible for deciding *whether* to refund (only when the original
    enqueue actually consumed a token AND the worker hasn't started);
    this function just performs the credit safely.

    A no-op when the bucket key is absent (TTL evicted — the user has
    long since recovered) or at capacity.

    Args:
        redis: App-namespaced Redis client (must have ``decode_responses=True``).
        guild_id: Discord guild ID; namespaces the bucket key.
        capacity: Burst allowance — refunds never push above this.
        refill_per_hour: Passive refill rate, tokens per hour. Used to
            apply the same elapsed-time refill ``acquire`` would, so the
            stored ``tokens`` reflects current effective balance.
    """
    key = _KEY.format(guild_id=guild_id)
    refill_rate = refill_per_hour / 3600.0
    ttl = _ttl_seconds(capacity, refill_per_hour)

    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw_tokens, raw_last = await pipe.hmget(key, "tokens", "last_refill")
                if raw_tokens is None:
                    # Bucket evicted via TTL — user has fully recovered, nothing to add.
                    await pipe.unwatch()
                    return
                now = time()
                elapsed = max(0.0, now - float(raw_last))
                effective = min(
                    float(capacity),
                    float(raw_tokens) + elapsed * refill_rate,
                )
                if effective >= float(capacity):
                    # Already at cap — refund is a no-op.
                    await pipe.unwatch()
                    return
                tokens = min(float(capacity), effective + 1.0)

                pipe.multi()
                pipe.hset(
                    key,
                    mapping={"tokens": tokens, "last_refill": now},
                )
                pipe.expire(key, ttl)
                await pipe.execute()
                return
            except WatchError:
                continue
