"""ARQ queue client used by the bot to enqueue background jobs."""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from config import settings


def redis_settings() -> RedisSettings:
    """Build ARQ ``RedisSettings`` from the configured ``REDIS_URL``."""
    return RedisSettings.from_dsn(settings.REDIS_URL)


async def open_pool() -> ArqRedis:
    """Open a Redis connection pool for enqueueing jobs."""
    return await create_pool(redis_settings())
