"""Branch tests for the /status and /cancel cog.

The cog reads from Redis (via the ``jobs`` module) and from the Taskiq
result backend (via ``AsyncTaskiqTask``). Both are mocked at the import
site so we never need a live broker, RabbitMQ, or Redis.

Auth matrix exercised in TestAuth:
    requester / bot owner / guild owner (same guild) / guild owner
    (cross-guild) / unrelated user
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader_bot.cogs.jobs import Jobs
from downloader_bot.download.jobs import JobMeta


def _meta(
    *,
    owner_user_id: int = 42,
    guild_id: int | None = 12345,
    channel_id: int = 555,
    enqueued_at_iso: str = "2026-05-18T12:00:00+00:00",
    enqueue_consumed_token: bool = True,
) -> JobMeta:
    return JobMeta(
        owner_user_id=owner_user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        enqueued_at_iso=enqueued_at_iso,
        enqueue_consumed_token=enqueue_consumed_token,
    )


def _last_embed(ctx):
    return ctx.send.await_args.kwargs["embed"]


async def _status(cog, ctx, task_id=None):
    await cog.status.callback(cog, ctx, task_id=task_id)


async def _cancel(cog, ctx, task_id=None):
    await cog.cancel.callback(cog, ctx, task_id=task_id)


@pytest.fixture
def patched_jobs(mocker):
    """Patch the ``jobs`` module references inside ``cogs.jobs``."""
    return {
        "latest_job_for_user": mocker.patch(
            "downloader_bot.cogs.jobs.jobs.latest_job_for_user",
            new_callable=AsyncMock,
        ),
        "get_job_meta": mocker.patch(
            "downloader_bot.cogs.jobs.jobs.get_job_meta",
            new_callable=AsyncMock,
        ),
    }


@pytest.fixture
def patched_task(mocker):
    """Patch ``AsyncTaskiqTask`` so each test controls is_ready / get_progress."""
    task = MagicMock()
    task.is_ready = AsyncMock(return_value=False)
    task.get_progress = AsyncMock(return_value=None)
    mocker.patch(
        "downloader_bot.cogs.jobs.AsyncTaskiqTask",
        return_value=task,
    )
    return task


@pytest.fixture
def patched_cancellation(mocker):
    """Patch the cancellation backend so cancel() / is_cancelled are observable."""
    backend = MagicMock()
    backend.is_cancelled = AsyncMock(return_value=False)
    backend.cancel = AsyncMock()
    mocker.patch("downloader_bot.cogs.jobs.cancellation_backend", backend)
    return backend


@pytest.fixture
def patched_refund(mocker):
    """Patch ratelimit.refund so cancel can be inspected without fakeredis."""
    return mocker.patch(
        "downloader_bot.cogs.jobs.ratelimit.refund",
        new_callable=AsyncMock,
    )


# --- /status -------------------------------------------------------------


class TestStatusLookup:
    async def test_no_args_no_active_job_renders_no_active_jobs(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
    ):
        patched_jobs["latest_job_for_user"].return_value = None
        cog = Jobs(mock_bot)

        await _status(cog, mock_context)

        # job_not_found(task_id=None) → "No active jobs" embed
        assert _last_embed(mock_context).title == "No active jobs"

    async def test_explicit_task_id_with_missing_meta_renders_generic_not_found(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
    ):
        # Valid uuid format but no meta → the SAME "Job not found" embed
        # an unauthorised access would produce (no info leak).
        patched_jobs["get_job_meta"].return_value = None
        cog = Jobs(mock_bot)

        await _status(
            cog,
            mock_context,
            task_id="0" * 32,
        )

        assert _last_embed(mock_context).title == "Job not found"

    async def test_malformed_task_id_treated_as_not_found(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
    ):
        # No UUID-format match → reject without ever touching Redis.
        cog = Jobs(mock_bot)

        await _status(cog, mock_context, task_id="*; DROP TABLE x;--")

        assert _last_embed(mock_context).title == "Job not found"
        # Defensive check: never reach Redis when input is malformed.
        patched_jobs["get_job_meta"].assert_not_awaited()

    async def test_real_taskiq_id_format_is_accepted(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
    ):
        # Regression guard: Taskiq generates task IDs via uuid4().hex
        # (32 hex chars, no hyphens). An earlier regex required the
        # 36-char hyphenated form and silently rejected every real ID.
        import uuid

        real_taskiq_id = uuid.uuid4().hex
        assert len(real_taskiq_id) == 32  # sanity check
        patched_jobs["get_job_meta"].return_value = _meta(owner_user_id=42)
        cog = Jobs(mock_bot)

        await _status(cog, mock_context, task_id=real_taskiq_id)

        # The meta lookup happened → the regex didn't reject the input.
        patched_jobs["get_job_meta"].assert_awaited_once()


class TestStatusPhaseRendering:
    @pytest.fixture
    def cog_with_my_job(self, mock_bot, patched_jobs):
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(owner_user_id=42)
        return Jobs(mock_bot)

    async def test_cancelled_outranks_everything(
        self,
        mock_context,
        cog_with_my_job,
        patched_task,
        patched_cancellation,
    ):
        # Even if the task somehow looks delivered, /status shows
        # "Cancelled" when the cancellation backend says so (the user's
        # intent wins).
        patched_cancellation.is_cancelled.return_value = True
        patched_task.is_ready.return_value = True

        await _status(cog_with_my_job, mock_context)

        embed = _last_embed(mock_context)
        assert embed.title == "Job cancelled"

    async def test_delivered_when_ready(
        self,
        mock_context,
        cog_with_my_job,
        patched_task,
        patched_cancellation,
    ):
        patched_task.is_ready.return_value = True

        await _status(cog_with_my_job, mock_context)

        assert _last_embed(mock_context).title == "Job delivered"

    async def test_queued_when_no_progress_yet(
        self,
        mock_context,
        cog_with_my_job,
        patched_task,
        patched_cancellation,
    ):
        # No progress + not ready = still queued in RabbitMQ. The task's
        # first line emits picked_up, so absence of any progress is
        # unambiguous.
        patched_task.is_ready.return_value = False
        patched_task.get_progress.return_value = None

        await _status(cog_with_my_job, mock_context)

        embed = _last_embed(mock_context)
        # Phase field present + says "Queued"
        phase_field = next(f for f in embed.fields if f.name == "Phase")
        assert "Queued" in phase_field.value

    async def test_streaming_renders_heartbeat_position_and_tally(
        self,
        mock_context,
        cog_with_my_job,
        patched_task,
        patched_cancellation,
    ):
        from datetime import UTC, datetime, timedelta

        # Streaming progress payload with all four fields.
        updated_at = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
        prog = MagicMock()
        prog.meta = {
            "phase": "stream",
            "attachments_done": 12,
            "bytes_streamed": 87_654_321,
            "history_fraction": 0.37,
            "updated_at": updated_at,
        }
        patched_task.get_progress.return_value = prog

        await _status(cog_with_my_job, mock_context)

        embed = _last_embed(mock_context)
        field_names = {f.name for f in embed.fields}
        # Phase + Heartbeat + Position + Tally
        assert {"Phase", "Heartbeat", "Position", "Tally"} <= field_names

    async def test_streaming_with_malformed_updated_at_renders_without_heartbeat(
        self,
        mock_context,
        cog_with_my_job,
        patched_task,
        patched_cancellation,
    ):
        # Bad ISO string shouldn't crash — heartbeat just doesn't render.
        prog = MagicMock()
        prog.meta = {
            "phase": "stream",
            "attachments_done": 1,
            "bytes_streamed": 100,
            "history_fraction": 0.1,
            "updated_at": "not-a-date",
        }
        patched_task.get_progress.return_value = prog

        await _status(cog_with_my_job, mock_context)

        embed = _last_embed(mock_context)
        field_names = {f.name for f in embed.fields}
        assert "Heartbeat" not in field_names
        assert "Phase" in field_names


# --- /cancel -------------------------------------------------------------


class TestCancelFlow:
    @pytest.fixture
    def cog_with_my_job(self, mock_bot, patched_jobs):
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=42,
            guild_id=12345,
            enqueue_consumed_token=True,
        )
        return Jobs(mock_bot)

    async def test_no_job_renders_not_found_and_does_not_cancel(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
    ):
        patched_jobs["latest_job_for_user"].return_value = None
        cog = Jobs(mock_bot)

        await _cancel(cog, mock_context)

        patched_cancellation.cancel.assert_not_awaited()
        assert _last_embed(mock_context).title == "No active jobs"

    async def test_cancel_renders_confirmation_embed(
        self,
        mock_context,
        cog_with_my_job,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        await _cancel(cog_with_my_job, mock_context)

        patched_cancellation.cancel.assert_awaited_once_with("task-xyz")
        assert _last_embed(mock_context).title == "Cancellation requested"


class TestCancelRefund:
    async def test_refund_fires_when_queued_and_token_was_consumed(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # consumed=True + not ready + no progress → refund eligible.
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=42,
            guild_id=12345,
            enqueue_consumed_token=True,
        )
        patched_task.is_ready.return_value = False
        patched_task.get_progress.return_value = None
        cog = Jobs(mock_bot)

        await _cancel(cog, mock_context)

        patched_refund.assert_awaited_once()
        assert patched_refund.await_args.args[1] == 12345  # guild_id

    async def test_refund_skipped_when_token_was_not_consumed(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Guild owner originally enqueued (bypass) → consumed=False.
        # Refunding would add a token that was never spent.
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=42,
            guild_id=12345,
            enqueue_consumed_token=False,
        )
        cog = Jobs(mock_bot)

        await _cancel(cog, mock_context)

        patched_refund.assert_not_awaited()
        patched_cancellation.cancel.assert_awaited_once()

    async def test_refund_skipped_when_worker_already_started(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Any progress at all = worker has the job. Refund would let
        # the user reclaim a token for work that already cost resources.
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=42,
            guild_id=12345,
            enqueue_consumed_token=True,
        )
        prog = MagicMock()
        prog.meta = {"phase": "picked_up"}
        patched_task.get_progress.return_value = prog
        cog = Jobs(mock_bot)

        await _cancel(cog, mock_context)

        patched_refund.assert_not_awaited()

    async def test_refund_skipped_for_dm_context_job(
        self,
        mock_bot,
        dm_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # DM-context job has guild_id=None (no bucket to refund to).
        # The cog must skip the refund cleanly even if other conditions
        # are met.
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=42,
            guild_id=None,
            enqueue_consumed_token=False,
        )
        cog = Jobs(mock_bot)

        await _cancel(cog, dm_context)

        patched_refund.assert_not_awaited()
        patched_cancellation.cancel.assert_awaited_once()

    async def test_refund_failure_does_not_block_cancel_confirmation(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Redis blip during refund is best-effort. Cancel already
        # succeeded; the user should still see the confirmation embed.
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=42,
            guild_id=12345,
            enqueue_consumed_token=True,
        )
        patched_refund.side_effect = RuntimeError("redis down")
        cog = Jobs(mock_bot)

        await _cancel(cog, mock_context)

        assert _last_embed(mock_context).title == "Cancellation requested"
        mock_bot.logger.warning.assert_called_once()


# --- Authorization matrix -----------------------------------------------


class TestAuth:
    async def test_requester_can_act(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # mock_context.author.id is 42; meta.owner_user_id is 42.
        patched_jobs["get_job_meta"].return_value = _meta(owner_user_id=42)
        cog = Jobs(mock_bot)

        await _status(
            cog,
            mock_context,
            task_id="a" * 32,
        )

        # Not the "Job not found" embed → request was authorised.
        assert _last_embed(mock_context).title != "Job not found"

    async def test_bot_owner_can_act_on_anyone(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Author is not the owner; bot.is_owner returns True.
        mock_bot.is_owner = AsyncMock(return_value=True)
        patched_jobs["get_job_meta"].return_value = _meta(owner_user_id=999)
        cog = Jobs(mock_bot)

        await _status(
            cog,
            mock_context,
            task_id="a" * 32,
        )

        assert _last_embed(mock_context).title != "Job not found"

    async def test_guild_owner_can_act_in_same_guild(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Author IS the guild owner; meta's guild_id matches.
        mock_context.guild.owner_id = mock_context.author.id
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=999,  # someone else
            guild_id=12345,  # same as ctx.guild.id
        )
        cog = Jobs(mock_bot)

        await _status(
            cog,
            mock_context,
            task_id="a" * 32,
        )

        assert _last_embed(mock_context).title != "Job not found"

    async def test_guild_owner_rejected_for_cross_guild_job(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Author IS the owner of guild 12345, but the job is in 99999.
        mock_context.guild.owner_id = mock_context.author.id
        patched_jobs["get_job_meta"].return_value = _meta(
            owner_user_id=999,
            guild_id=99999,
        )
        cog = Jobs(mock_bot)

        await _status(
            cog,
            mock_context,
            task_id="a" * 32,
        )

        # Same generic "Job not found" embed — no info leak that the
        # job exists in another guild.
        assert _last_embed(mock_context).title == "Job not found"

    async def test_unrelated_user_rejected(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Not the requester, not bot owner, not the guild owner.
        patched_jobs["get_job_meta"].return_value = _meta(owner_user_id=999)
        cog = Jobs(mock_bot)

        await _cancel(
            cog,
            mock_context,
            task_id="a" * 32,
        )

        # /cancel must not fire when auth rejects.
        patched_cancellation.cancel.assert_not_awaited()
        assert _last_embed(mock_context).title == "Job not found"


# --- Double-cancel idempotency ------------------------------------------


class TestDoubleCancel:
    async def test_double_cancel_still_routes_through_backend(
        self,
        mock_bot,
        mock_context,
        patched_jobs,
        patched_task,
        patched_cancellation,
        patched_refund,
    ):
        # Backend is idempotent; the cog just keeps calling cancel().
        # The user gets the same confirmation each time.
        patched_jobs["latest_job_for_user"].return_value = "task-xyz"
        patched_jobs["get_job_meta"].return_value = _meta(
            enqueue_consumed_token=False,  # skip the refund branch
        )
        cog = Jobs(mock_bot)

        await _cancel(cog, mock_context)
        await _cancel(cog, mock_context)

        assert patched_cancellation.cancel.await_count == 2
