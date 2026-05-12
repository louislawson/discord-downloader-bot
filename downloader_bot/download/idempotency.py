"""Redis-backed idempotency for the download pipeline.

Keys are scoped by ``task_id``. Taskiq preserves task_id across retries
(via SimpleRetryMiddleware), so a retried attempt sees the same Redis
state and skips already-completed phases.

TTL aligns with SAS URL retention (default 24h, per guild_settings):
if a retry happens beyond that window, the SAS URL has expired anyway,
so re-uploading is the correct action.
"""

from redis.asyncio import Redis

_ARCHIVE_URL_KEY = "task:{task_id}:archive_url"
_DELIVERED_KEY = "task:{task_id}:delivered"


async def get_cached_archive_url(redis: Redis, task_id: str) -> str | None:
    """Return the URL from a prior successful upload in this task lineage."""
    return await redis.get(_ARCHIVE_URL_KEY.format(task_id=task_id))


async def cache_archive_url(
    redis: Redis,
    task_id: str,
    url: str,
    ttl_seconds: int,
) -> None:
    """Mark this task as 'upload complete'. Retries skip the upload phase."""
    await redis.set(
        _ARCHIVE_URL_KEY.format(task_id=task_id),
        url,
        ex=ttl_seconds,
    )


async def is_delivered(redis: Redis, task_id: str) -> bool:
    """True if the user has already received this archive."""
    return bool(await redis.exists(_DELIVERED_KEY.format(task_id=task_id)))


async def mark_delivered(
    redis: Redis,
    task_id: str,
    ttl_seconds: int,
) -> None:
    """Mark this task as 'delivery complete'. Retries skip the deliver phase."""
    await redis.set(
        _DELIVERED_KEY.format(task_id=task_id),
        "1",
        ex=ttl_seconds,
    )
