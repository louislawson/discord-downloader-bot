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
    """Return the URL cached for this task lineage, if any.

    Args:
        redis: App-namespaced Redis client.
        task_id: Taskiq task ID (preserved across retries).

    Returns:
        The URL written by a prior successful upload (key
        ``task:{task_id}:archive_url``), or ``None`` if no key exists.
    """
    return await redis.get(_ARCHIVE_URL_KEY.format(task_id=task_id))


async def cache_archive_url(
    redis: Redis,
    task_id: str,
    url: str,
    ttl_seconds: int,
) -> None:
    """Mark the upload phase complete by caching the SAS URL.

    Subsequent retries with the same ``task_id`` skip the upload phase.

    Args:
        redis: App-namespaced Redis client.
        task_id: Taskiq task ID.
        url: The SAS URL produced by ``storage.upload_and_sign``.
        ttl_seconds: Lifetime of the cache entry; should match the SAS URL's
            TTL so retries past expiry correctly re-upload.
    """
    await redis.set(
        _ARCHIVE_URL_KEY.format(task_id=task_id),
        url,
        ex=ttl_seconds,
    )


async def is_delivered(redis: Redis, task_id: str) -> bool:
    """Return whether this task has already been delivered to the user.

    Args:
        redis: App-namespaced Redis client.
        task_id: Taskiq task ID.

    Returns:
        ``True`` if the ``task:{task_id}:delivered`` key is set.
    """
    return bool(await redis.exists(_DELIVERED_KEY.format(task_id=task_id)))


async def mark_delivered(
    redis: Redis,
    task_id: str,
    ttl_seconds: int,
) -> None:
    """Mark the delivery phase complete; subsequent retries skip delivery.

    Set this *after* the send returns — a crash before this point means a
    retry re-delivers (a duplicate DM beats no DM).

    Args:
        redis: App-namespaced Redis client.
        task_id: Taskiq task ID.
        ttl_seconds: Lifetime of the cache entry; should match the SAS URL's
            TTL.
    """
    await redis.set(
        _DELIVERED_KEY.format(task_id=task_id),
        "1",
        ex=ttl_seconds,
    )
