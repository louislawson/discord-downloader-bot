"""Worker liveness — heartbeat middleware + CLI probe.

Replaces `arq --check`. The middleware writes a per-PID sentinel key into
Redis with a short TTL on every job event and on an idle-refresh loop;
the CLI probe (run by docker-compose HEALTHCHECK) exits 0 iff at least
one fresh sentinel exists.
"""

import asyncio
import contextlib
import logging
import os
import sys
from typing import Any

import redis.asyncio as redis
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from downloader_bot.config import settings

logger = logging.getLogger("app.worker.healthcheck")

_HEARTBEAT_TTL_SECONDS = 90  # docker healthcheck interval is 60s
_HEARTBEAT_REFRESH_SECONDS = 30  # refresh cadence for idle workers


class HeartbeatMiddleware(TaskiqMiddleware):
    """Refresh `worker:heartbeat:<pid>` on every job event + every 30s."""

    def __init__(self, redis_url: str) -> None:
        """Compose the per-PID heartbeat key from ``TASKIQ_PROCESS_ROLE``.

        Args:
            redis_url: URL of the Redis instance the sentinel is written to.
        """
        super().__init__()
        self._redis_url = redis_url
        self._role = os.environ.get("TASKIQ_PROCESS_ROLE", "worker")
        self._key = f"taskiq:heartbeat:{self._role}:{os.getpid()}"
        self._client: redis.Redis | None = None
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """Open the Redis client, write the first sentinel, start the idle loop."""
        self._client = redis.from_url(self._redis_url, decode_responses=True)
        await self._refresh()
        self._task = asyncio.create_task(self._loop(), name="heartbeat-loop")

    async def shutdown(self) -> None:
        """Cancel the idle loop, delete this PID's sentinel, close the client."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._client is not None:
            try:
                await self._client.delete(self._key)
            finally:
                await self._client.aclose()

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        """Refresh the sentinel before a task starts.

        Args:
            message: The Taskiq message about to be executed.

        Returns:
            The message, unchanged.
        """
        await self._refresh()
        return message

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        """Refresh the sentinel after a task finishes.

        Args:
            message: The Taskiq message that was executed.
            result: The execution result (success or failure).
        """
        await self._refresh()

    async def _loop(self) -> None:
        """Refresh the sentinel every ``_HEARTBEAT_REFRESH_SECONDS`` while idle.

        Without this, an idle worker would let the sentinel TTL expire and
        the docker healthcheck would mark it unhealthy.
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_REFRESH_SECONDS)
            await self._refresh()

    async def _refresh(self) -> None:
        """Write the sentinel with a fresh TTL; swallow Redis errors.

        A failed heartbeat write must never fail a job — at worst the
        sentinel TTL expires and the next docker healthcheck restarts the
        worker, which is the correct outcome.
        """
        if self._client is None:
            return
        try:
            await self._client.set(self._key, "1", ex=_HEARTBEAT_TTL_SECONDS)
        except redis.RedisError:
            # Don't fail jobs over a heartbeat write — log and move on.
            logger.exception("Heartbeat refresh failed")


# --- CLI probe (`python -m app.worker.healthcheck`) ---------------


async def _check(role: str) -> int:
    """Scan Redis for any fresh sentinel keyed by ``role``.

    Args:
        role: The process role to probe (e.g. ``worker`` or ``scheduler``).

    Returns:
        ``0`` if at least one fresh sentinel exists for the role, ``1`` otherwise.
    """
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        async for _ in client.scan_iter(
            match=f"taskiq:heartbeat:{role}:*",
        ):
            return 0
        return 1
    finally:
        await client.aclose()


def main() -> None:
    """CLI entry point; exits with the result of :func:`_check`."""
    role = os.environ.get("TASKIQ_PROCESS_ROLE", "worker")
    sys.exit(asyncio.run(_check(role)))


if __name__ == "__main__":
    main()
