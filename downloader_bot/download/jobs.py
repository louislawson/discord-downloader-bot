"""Redis-backed user→jobs index for ``/status`` and ``/cancel``.

The Taskiq result backend already stores task results and progress, but it
doesn't know *which user* enqueued a given ``task_id``. This module is the
small parallel index that lets a user find "my latest job" without
copy-pasting the ID, and lets the cancel path check ownership before
firing ``cancellation_backend.cancel(task_id)``.

Two key shapes, both alongside the existing
``task:{task_id}:archive_url`` / ``:delivered`` keys:

- ``task:{task_id}:meta`` (JSON) — owner / guild / channel / enqueued-at,
  plus whether the original enqueue consumed a rate-limit token (so the
  cancel path can decide whether a refund is due).
- ``user:{user_id}:active_jobs`` (ZSET, score = unix timestamp) — the
  user's currently-known jobs. ``ZREVRANGE 0 0`` gives "latest."

# AUTH-BOUNDARY: ``get_job_meta`` returns raw owner/guild/channel data
# with no permission check. Callers MUST go through
# ``downloader_bot.cogs.jobs.Jobs._resolve_job`` so the requester /
# guild-owner / bot-owner auth matrix is applied. Grep this marker
# before adding a new caller.
"""

import json
import time
from typing import TypedDict

from redis.asyncio import Redis

_META_KEY = "task:{task_id}:meta"
_USER_JOBS_KEY = "user:{user_id}:active_jobs"


class JobMeta(TypedDict):
    """Payload stored under ``task:{task_id}:meta`` and read by ``/status``.

    ``enqueue_consumed_token`` records whether the cog's
    ``ratelimit.acquire`` actually consumed a token at enqueue time
    (False for DM-context — no bucket — and guild-owner-bypass enqueues).
    The cancel path refunds only when this is True, otherwise we'd add a
    token that was never spent.
    """

    owner_user_id: int
    guild_id: int | None
    channel_id: int
    enqueued_at_iso: str
    enqueue_consumed_token: bool


async def record_job(
    redis: Redis,
    task_id: str,
    *,
    meta: JobMeta,
    ttl_seconds: int,
) -> None:
    """Atomically register a newly-enqueued job in both index keys.

    Uses ``pipeline(transaction=True)`` so a connection drop between the
    ZADD and the SET (or either EXPIRE) can't leave a half-state — same
    shape as ``ratelimit.acquire``. Without atomicity, a ZADD that
    succeeded without its EXPIRE would leak a key per affected user.

    Args:
        redis: App-namespaced Redis client.
        task_id: The Taskiq task ID returned by ``download_channel_media.kiq``.
        meta: Owner/guild/channel/enqueue-state payload.
        ttl_seconds: Lifetime of both keys; should match the SAS URL
            retention so the index expires alongside the archive.
    """
    meta_key = _META_KEY.format(task_id=task_id)
    user_jobs_key = _USER_JOBS_KEY.format(user_id=meta["owner_user_id"])
    now = time.time()

    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(meta_key, json.dumps(meta), ex=ttl_seconds)
        pipe.zadd(user_jobs_key, {task_id: now})
        pipe.expire(user_jobs_key, ttl_seconds)
        await pipe.execute()


async def get_job_meta(redis: Redis, task_id: str) -> JobMeta | None:
    """Return the stored job meta. **No auth check** — see AUTH-BOUNDARY above.

    Only ``downloader_bot.cogs.jobs.Jobs._resolve_job`` should call this
    in production code; that's where the requester / guild-owner /
    bot-owner check lives. Tests can call it freely.

    Args:
        redis: App-namespaced Redis client.
        task_id: Taskiq task ID.

    Returns:
        The decoded ``JobMeta``, or ``None`` if no key exists.
    """
    raw = await redis.get(_META_KEY.format(task_id=task_id))
    if raw is None:
        return None
    return json.loads(raw)


async def latest_job_for_user(redis: Redis, user_id: int) -> str | None:
    """Return the most-recently-enqueued active task_id for ``user_id``.

    Args:
        redis: App-namespaced Redis client.
        user_id: Discord user ID.

    Returns:
        The task_id with the highest ZSET score (latest ``record_job``),
        or ``None`` if the user has no active jobs.
    """
    members = await redis.zrevrange(
        _USER_JOBS_KEY.format(user_id=user_id),
        0,
        0,
    )
    return members[0] if members else None


async def forget_job(redis: Redis, task_id: str, owner_user_id: int) -> None:
    """Drop a finished job from both index keys.

    Called from the task's success path after delivery — failed retry
    attempts deliberately don't forget, because Taskiq retries with the
    same task_id and forgetting on attempt 1 would make ``/status``
    return "no active jobs" while attempt 2 is still running.

    Idempotent: missing keys are silently fine.

    Args:
        redis: App-namespaced Redis client.
        task_id: Taskiq task ID.
        owner_user_id: The user whose ZSET entry to remove.
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(_META_KEY.format(task_id=task_id))
        pipe.zrem(_USER_JOBS_KEY.format(user_id=owner_user_id), task_id)
        await pipe.execute()
