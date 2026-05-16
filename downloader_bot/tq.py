"""Taskiq broker, scheduler, cancellation backend and typed dependency providers.

Single source of truth for the queue infrastructure shared between the bot
(publishes tasks) and the Taskiq worker / scheduler (consume / schedule).

The ``WORKER_STARTUP`` hook attaches shared resources (discord REST client,
aiohttp session, storage backend, asyncpg pool, repo, Redis) to the per-worker
``state``. Tasks should read those via the typed providers below
(``get_storage``, ``get_redis``, etc.) rather than touching ``context.state``
directly — providers give the type checker the right type and let tests pass
plain mocks.
"""

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
    """Open per-worker shared resources and attach them to ``state``.

    Anything stashed here must have a matching typed provider below so tasks
    can declare it via ``Annotated[T, TaskiqDepends(provider)]``.
    """
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
    """Release per-worker shared resources in reverse order of startup."""
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
    """Return the worker-shared discord.py REST client.

    Args:
        context: Injected Taskiq context.

    Returns:
        The REST-only ``discord.Client`` opened in ``WORKER_STARTUP``.
    """
    return context.state.discord_client


def get_download_session(
    context: Annotated[Context, TaskiqDepends()],
) -> aiohttp.ClientSession:
    """Return the worker-shared aiohttp session used by the zip pipeline.

    Args:
        context: Injected Taskiq context.

    Returns:
        The ``aiohttp.ClientSession`` opened in ``WORKER_STARTUP``.
    """
    return context.state.download_session


def get_storage(
    context: Annotated[Context, TaskiqDepends()],
) -> StorageBackend:
    """Return the worker-shared storage backend.

    Args:
        context: Injected Taskiq context.

    Returns:
        The storage backend entered into the worker's ``AsyncExitStack``.
    """
    return context.state.storage


def get_guild_settings_repo(
    context: Annotated[Context, TaskiqDepends()],
) -> GuildSettingsRepo:
    """Return the worker-shared per-guild settings repo (asyncpg-backed).

    Args:
        context: Injected Taskiq context.

    Returns:
        The ``GuildSettingsRepo`` constructed in ``WORKER_STARTUP``.
    """
    return context.state.guild_settings_repo


def get_redis(
    context: Annotated[Context, TaskiqDepends()],
) -> Redis:
    """Return the worker-shared Redis client used for idempotency state.

    Args:
        context: Injected Taskiq context.

    Returns:
        The app-namespaced Redis client (distinct from the Taskiq result
        backend's internal Redis).
    """
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
