"""Job-control commands: ``/status`` and ``/cancel``.

Two top-level hybrid commands share a cog because they share the same
authorization helper (``_resolve_job``) and the same lookup-by-task-id
plumbing. Both default to "your latest active job" when called without a
``task_id`` argument; both accept an explicit ``task_id`` pasted from the
``/download`` ack embed footer.

Authorization: the requester can always see and cancel their own job; a
guild owner can act on jobs in their own guild; the bot owner can act on
anything. Anyone else (including a guild-A owner who somehow learns a
guild-B task_id) gets the same generic "Job not found" response a
genuinely-missing task would produce — we deliberately don't distinguish
404 from 403 so leaked task IDs can't be used to probe.

The cog reads the Taskiq result-backend keys directly via
``AsyncTaskiqTask`` — ``is_ready()`` and ``get_progress()`` are both
cheap, non-destructive reads. We never call ``get_result()`` because the
broker is configured with ``keep_results=False`` (see
``downloader_bot/tq.py``); that would consume the result and break the
worker's delivery promise.
"""

import re
from datetime import UTC, datetime
from typing import cast

from discord.ext import commands
from discord.ext.commands import Context
from taskiq import AsyncTaskiqTask

from downloader_bot.config import settings
from downloader_bot.download import jobs, ratelimit
from downloader_bot.download.jobs import JobMeta
from downloader_bot.embeds import job_cancelled, job_not_found, job_status
from downloader_bot.tasks.download import ProgressMeta
from downloader_bot.tq import broker, cancellation_backend

# Taskiq generates task IDs via ``uuid4().hex`` — 32 hex chars, no hyphens
# (NOT the 36-char hyphenated form). Validate the format before touching
# Redis so a malformed task_id can't be used to probe key namespaces.
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


class Jobs(commands.Cog, name="jobs"):
    """``/status`` and ``/cancel`` commands."""

    def __init__(self, bot) -> None:
        """Bind the cog to its parent bot."""
        self.bot = bot

    async def _resolve_job(
        self,
        context: Context,
        task_id: str | None,
    ) -> tuple[str, JobMeta] | None:
        """Resolve a target job + run the auth check.

        Returns ``(task_id, meta)`` if the caller is allowed to act on
        the job, else ``None``. Callers render
        :func:`downloader_bot.embeds.job_not_found` on a ``None``
        result, with ``task_id=None`` for the no-args case so the
        embed says "no active jobs" instead of "job not found."

        Args:
            context: The command context.
            task_id: Optional explicit task ID from the user. When
                ``None``, the helper looks up the requester's latest
                active job.

        Returns:
            ``(task_id, meta)`` on success, ``None`` on missing /
            unauthorised / malformed.
        """
        if task_id is None:
            task_id = await jobs.latest_job_for_user(
                self.bot.redis,
                context.author.id,
            )
            if task_id is None:
                return None
        elif not _TASK_ID_RE.match(task_id):
            return None

        meta = await jobs.get_job_meta(self.bot.redis, task_id)
        if meta is None:
            return None

        if not await self._can_act_on(context, meta):
            return None

        return task_id, meta

    async def _can_act_on(self, context: Context, meta: JobMeta) -> bool:
        """Return ``True`` iff the caller may inspect or cancel this job."""
        if meta["owner_user_id"] == context.author.id:
            return True
        if await self.bot.is_owner(context.author):
            return True
        return (
            context.guild is not None
            and meta["guild_id"] is not None
            and meta["guild_id"] == context.guild.id
            and context.author.id == context.guild.owner_id
        )

    @commands.hybrid_command(
        name="status",
        description="Check the status of your latest download job.",
    )
    async def status(
        self,
        context: Context,
        task_id: str | None = None,
    ) -> None:
        """Render the current state of a job.

        Args:
            context: The command context.
            task_id: Optional Taskiq task ID from the /download ack
                footer. Defaults to your most recent active job.
        """
        await context.defer(ephemeral=True)

        resolved = await self._resolve_job(context, task_id)
        if resolved is None:
            await context.send(
                embed=job_not_found(task_id=task_id),
                ephemeral=True,
            )
            return
        target_id, meta = resolved

        # Order matters: cancelled outranks delivered (a job that
        # finished after cancellation was requested still gets the
        # "cancelled" label since that's what the user asked for).
        if await cancellation_backend.is_cancelled(target_id):
            phase = "cancelled"
            embed = job_status(
                task_id=target_id,
                phase=phase,
                requester_id=meta["owner_user_id"],
            )
            await context.send(embed=embed, ephemeral=True)
            return

        task: AsyncTaskiqTask = AsyncTaskiqTask(
            target_id,
            broker.result_backend,
        )

        if await task.is_ready():
            embed = job_status(
                task_id=target_id,
                phase="done",
                requester_id=meta["owner_user_id"],
            )
            await context.send(embed=embed, ephemeral=True)
            return

        progress = await task.get_progress()
        if progress is None:
            # No progress yet → still queued in RabbitMQ. The task body's
            # first line emits picked_up, so absence of progress is
            # unambiguous.
            embed = job_status(
                task_id=target_id,
                phase="queued",
                requester_id=meta["owner_user_id"],
            )
            await context.send(embed=embed, ephemeral=True)
            return

        progress_meta = cast(ProgressMeta, progress.meta or {})
        phase = progress_meta.get("phase", "queued")

        heartbeat_age: float | None = None
        if (updated_at := progress_meta.get("updated_at")) is not None:
            try:
                ts = datetime.fromisoformat(updated_at)
                heartbeat_age = (datetime.now(UTC) - ts).total_seconds()
            except ValueError:
                heartbeat_age = None

        embed = job_status(
            task_id=target_id,
            phase=phase,
            requester_id=meta["owner_user_id"],
            attachments_done=progress_meta.get("attachments_done"),
            bytes_streamed=progress_meta.get("bytes_streamed"),
            history_fraction=progress_meta.get("history_fraction"),
            heartbeat_age_seconds=heartbeat_age,
        )
        await context.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(
        name="cancel",
        description="Cancel one of your in-flight download jobs.",
    )
    async def cancel(
        self,
        context: Context,
        task_id: str | None = None,
    ) -> None:
        """Request cancellation of a job.

        The rate-limit bucket is refunded one token only when the
        original enqueue actually consumed one (DM-context and
        guild-owner enqueues bypass the bucket) AND the worker hadn't
        yet picked up the job (any progress emitted means the worker is
        already chewing through real resources).

        Args:
            context: The command context.
            task_id: Optional Taskiq task ID from the /download ack
                footer. Defaults to your most recent active job.
        """
        await context.defer(ephemeral=True)

        resolved = await self._resolve_job(context, task_id)
        if resolved is None:
            await context.send(
                embed=job_not_found(task_id=task_id),
                ephemeral=True,
            )
            return
        target_id, meta = resolved

        # Cancel FIRST, then snapshot. cancel() is idempotent at the
        # backend; once the state-holder flag is set, any further worker
        # pickup is short-circuited by CancelAwareRetryMiddleware. The
        # snapshot afterward truly reflects "did the worker get there
        # before our cancel landed?" — sharper refund signal than
        # snapshotting first would give.
        await cancellation_backend.cancel(target_id)

        task: AsyncTaskiqTask = AsyncTaskiqTask(
            target_id,
            broker.result_backend,
        )
        ready = await task.is_ready()
        progress = await task.get_progress()
        refund_eligible = (
            meta["enqueue_consumed_token"] and not ready and progress is None
        )

        if refund_eligible and meta["guild_id"] is not None:
            try:
                await ratelimit.refund(
                    self.bot.redis,
                    meta["guild_id"],
                    capacity=settings.GUILD_RATE_LIMIT_BURST,
                    refill_per_hour=settings.GUILD_RATE_LIMIT_PER_HOUR,
                )
            except Exception as exc:
                self.bot.logger.warning(
                    "ratelimit.refund failed for task %s: %s",
                    target_id,
                    exc,
                )

        await context.send(
            embed=job_cancelled(
                task_id=target_id,
                requester_id=meta["owner_user_id"],
            ),
            ephemeral=True,
        )


async def setup(bot) -> None:
    """Extension entry point; called by ``bot.load_extension``.

    Args:
        bot: The bot instance to load this cog into.
    """
    await bot.add_cog(Jobs(bot))
