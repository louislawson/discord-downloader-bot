"""Tests for the broker wiring in ``downloader_bot.tq``.

Currently covers ``CancelAwareRetryMiddleware`` — the small
``SimpleRetryMiddleware`` subclass that prevents ``/cancel`` from
triggering a retry storm against the cancellation-state flag.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from taskiq_cancellation.exceptions import TaskCancellationException

from downloader_bot.tq import CancelAwareRetryMiddleware


def _message(task_id: str = "task-abc", retry_on_error: bool = True):
    """Minimal TaskiqMessage stand-in for the middleware's on_error path."""
    msg = MagicMock()
    msg.task_id = task_id
    msg.task_name = "download_channel_media"
    msg.labels = {"retry_on_error": retry_on_error}
    msg.args = ()
    msg.kwargs = {}
    return msg


def _result():
    """Minimal TaskiqResult stand-in (the middleware assigns .error on retry)."""
    return MagicMock()


@pytest.fixture
def middleware(mocker):
    """Middleware wired to a broker mock; AsyncKicker is patched so retries
    don't actually try to publish to RabbitMQ."""
    mw = CancelAwareRetryMiddleware(default_retry_count=3)
    mw.broker = MagicMock()
    # Patch AsyncKicker at the import site so `.kiq(...)` is observable.
    kicker = MagicMock()
    kicker.with_task_id = MagicMock(return_value=kicker)
    kicker.with_labels = MagicMock(return_value=kicker)
    kicker.kiq = AsyncMock()
    mocker.patch(
        "taskiq.middlewares.simple_retry_middleware.AsyncKicker",
        return_value=kicker,
    )
    mw._kicker_mock = kicker  # expose for assertions
    return mw


class TestCancelAwareRetry:
    async def test_task_cancellation_exception_does_not_retry(self, middleware):
        # /cancel flipped the state flag; the next pickup raised
        # TaskCancellationException. We must NOT re-kick — otherwise the
        # cancellation_backend cancels it again and we loop until
        # default_retry_count is exhausted, polluting logs each round.
        await middleware.on_error(
            _message(),
            _result(),
            TaskCancellationException(),
        )

        middleware._kicker_mock.kiq.assert_not_awaited()

    async def test_other_exceptions_still_retry(self, middleware):
        # Anything that isn't a cancellation should still go through the
        # parent SimpleRetryMiddleware retry path (transient broker/network/
        # storage failures must keep retrying as before).
        await middleware.on_error(
            _message(),
            _result(),
            RuntimeError("transient azure 500"),
        )

        middleware._kicker_mock.kiq.assert_awaited_once()

    async def test_retry_disabled_on_label_still_skips_for_cancellation(
        self,
        middleware,
    ):
        # Even with retry_on_error=False on the label, the cancellation
        # branch should short-circuit cleanly (no exception, no retry).
        await middleware.on_error(
            _message(retry_on_error=False),
            _result(),
            TaskCancellationException(),
        )

        middleware._kicker_mock.kiq.assert_not_awaited()
