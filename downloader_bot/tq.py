from contextlib import AsyncExitStack
from typing import Annotated

import aiohttp
import discord
from redis.asyncio import Redis
from taskiq import Context, TaskiqDepends, TaskiqEvents, TaskiqScheduler
from taskiq.middlewares.simple_retry_middleware import SimpleRetryMiddleware
from taskiq.middlewares.taskiq_admin_middleware import TaskiqAdminMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_aio_pika import AioPikaBroker
from taskiq_cancellation import ModularCancellationBackend
from taskiq_cancellation.notifiers.aiopika import AioPikaNotifier
from taskiq_cancellation.state_holders.redis import RedisCancellationStateHolder
from taskiq_redis import RedisAsyncResultBackend, RedisScheduleSource

from downloader_bot.config import settings
from downloader_bot.db.guild_settings import GuildSettingsRepo
from downloader_bot.db.pool import build_pool, close_pool
from downloader_bot.download.discord_rest import close_client, open_client
from downloader_bot.storage import get_storage_backend
from downloader_bot.storage.base import StorageBackend
from downloader_bot.worker.healthcheck import HeartbeatMiddleware

broker = (
    AioPikaBroker(
        f"amqp://{settings.RABBITMQ_DEFAULT_USER}:{settings.RABBITMQ_DEFAULT_PASS}@rabbitmq:5672",
    )
    .with_result_backend(
        RedisAsyncResultBackend(
            settings.REDIS_URL,
            result_ex_time=86400,
            keep_results=False,
        )
    )
    .with_middlewares(
        TaskiqAdminMiddleware(
            url=settings.TASKIQ_ADMIN_URL or "",
            api_token=settings.TASKIQ_ADMIN_API_TOKEN or "",
            taskiq_broker_name=settings.TASKIQ_ADMIN_BROKER_NAME or "",
        ),
        HeartbeatMiddleware(redis_url=settings.REDIS_URL),
        SimpleRetryMiddleware(default_retry_count=3),
    )
)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _on_worker_startup(state) -> None:
    state.discord_client = await open_client(settings.TOKEN)
    # Separate session for streaming attachment downloads. We don't reuse
    # discord.py's HTTPClient session because we need direct
    # ``resp.content.iter_chunked`` access for the zip pipeline.
    state.download_session = aiohttp.ClientSession()
    # AsyncExitStack lets us enter the StorageBackend context manager once
    # per worker (keeping the Azure ContainerClient connection pool warm)
    # without manually calling __aenter__/__aexit__ on the backend itself.
    state.exit_stack = AsyncExitStack()
    state.storage = await state.exit_stack.enter_async_context(get_storage_backend())
    # asyncpg pool + repo for per-guild settings lookup.
    state.db_pool = await build_pool()
    state.guild_settings_repo = GuildSettingsRepo(state.db_pool)
    # Redis client for idempotency state (separate from Taskiq's internal
    # result-backend Redis — we want our app namespace isolated).
    state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _on_worker_shutdown(state) -> None:
    await state.exit_stack.aclose()
    await state.download_session.close()
    await close_client(state.discord_client)
    await close_pool(state.db_pool)
    await state.redis.aclose()


# --- Typed dependency providers ---------------------------------------------
#
# `TaskiqState` is a dynamic attribute container — anything we stash on it
# in WORKER_STARTUP is invisible to type checkers. The Taskiq-recommended
# fix is a provider function per shared resource; tasks then declare what
# they need via ``Annotated[Type, TaskiqDepends(provider)]`` and get full
# IDE autocomplete + simpler unit tests (mocks pass directly as args).


def get_discord_client(
    context: Annotated[Context, TaskiqDepends()],
) -> discord.Client:
    """Provider for the worker-shared discord.py REST client."""
    return context.state.discord_client


def get_download_session(
    context: Annotated[Context, TaskiqDepends()],
) -> aiohttp.ClientSession:
    """Provider for the worker-shared aiohttp session used by zip_stream."""
    return context.state.download_session


def get_storage(
    context: Annotated[Context, TaskiqDepends()],
) -> StorageBackend:
    """Provider for the worker-shared storage backend."""
    return context.state.storage


def get_guild_settings_repo(
    context: Annotated[Context, TaskiqDepends()],
) -> GuildSettingsRepo:
    """Provider for the worker-shared GuildSettingsRepo (asyncpg-backed)."""
    return context.state.guild_settings_repo


def get_redis(
    context: Annotated[Context, TaskiqDepends()],
) -> Redis:
    """Provider for the worker-shared Redis client (idempotency state)."""
    return context.state.redis


cancellation_backend = ModularCancellationBackend(
    RedisCancellationStateHolder(settings.REDIS_URL),
    AioPikaNotifier(
        f"amqp://{settings.RABBITMQ_DEFAULT_USER}:{settings.RABBITMQ_DEFAULT_PASS}@rabbitmq:5672",
    ),
).with_broker(broker)

redis_source = RedisScheduleSource(settings.REDIS_URL)

scheduler = TaskiqScheduler(
    broker,
    [
        redis_source,
        LabelScheduleSource(broker),
    ],
)
