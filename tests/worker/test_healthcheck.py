"""Tests for ``downloader_bot.worker.healthcheck`` — heartbeat middleware + CLI probe.

Replaces what ``arq --check`` gave us out of the box. The middleware writes
a per-PID sentinel ``taskiq:heartbeat:<role>:<pid>`` to Redis; the CLI
probe scans for any fresh matching key and exits 0/1.

Uses ``fakeredis.aioredis`` so tests run without a real Redis. ``os.getpid``
is patched in the lifecycle tests so the key shape is deterministic.
"""

from unittest.mock import patch

import fakeredis.aioredis
import pytest

from downloader_bot.worker.healthcheck import (
    _HEARTBEAT_TTL_SECONDS,
    HeartbeatMiddleware,
    _check,
)


@pytest.fixture
def fake_redis_factory(monkeypatch):
    """Patch the module-level ``redis.from_url`` to return a fresh fake.

    Returns the fake so tests can introspect / pre-seed it.
    """
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "downloader_bot.worker.healthcheck.redis.from_url",
        lambda *_a, **_k: fake,
    )
    return fake


# --- Key shape -------------------------------------------------------------


class TestKeyShape:
    def test_default_role_is_worker(self, monkeypatch):
        monkeypatch.delenv("TASKIQ_PROCESS_ROLE", raising=False)
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=12345):
            mw = HeartbeatMiddleware(redis_url="redis://x")

        assert mw._key == "taskiq:heartbeat:worker:12345"

    def test_role_from_env(self, monkeypatch):
        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "scheduler")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=99):
            mw = HeartbeatMiddleware(redis_url="redis://x")

        assert mw._key == "taskiq:heartbeat:scheduler:99"

    def test_role_isolation_per_process(self, monkeypatch):
        # Worker and scheduler running in the same container set the env
        # differently → their keys live in disjoint namespaces, so a
        # probe scoped to one role can't be fooled by the other.
        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "worker")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=1):
            worker_mw = HeartbeatMiddleware(redis_url="redis://x")

        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "scheduler")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=2):
            scheduler_mw = HeartbeatMiddleware(redis_url="redis://x")

        assert worker_mw._key != scheduler_mw._key
        assert worker_mw._key.split(":")[2] == "worker"
        assert scheduler_mw._key.split(":")[2] == "scheduler"


# --- Lifecycle -------------------------------------------------------------


class TestStartup:
    async def test_writes_key_with_ttl(self, fake_redis_factory, monkeypatch):
        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "worker")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=1):
            mw = HeartbeatMiddleware(redis_url="redis://x")
            try:
                await mw.startup()
                assert await fake_redis_factory.get(mw._key) == "1"
                ttl = await fake_redis_factory.ttl(mw._key)
                assert 0 < ttl <= _HEARTBEAT_TTL_SECONDS
            finally:
                await mw.shutdown()


class TestShutdown:
    async def test_deletes_key_on_clean_shutdown(
        self,
        fake_redis_factory,
        monkeypatch,
    ):
        # Proactive delete: the key is gone the moment the worker stops,
        # so a probe doesn't briefly see "healthy" after the process is gone.
        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "worker")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=1):
            mw = HeartbeatMiddleware(redis_url="redis://x")
            await mw.startup()
            key = mw._key
            await mw.shutdown()

        assert await fake_redis_factory.get(key) is None

    async def test_shutdown_cancels_loop_task(
        self,
        fake_redis_factory,
        monkeypatch,
    ):
        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "worker")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=1):
            mw = HeartbeatMiddleware(redis_url="redis://x")
            await mw.startup()
            loop_task = mw._task
            assert loop_task is not None
            await mw.shutdown()

        assert loop_task.cancelled() or loop_task.done()


# --- Refresh on job events -------------------------------------------------


class TestEventHooks:
    async def test_pre_execute_returns_message_unchanged(
        self,
        fake_redis_factory,
        monkeypatch,
    ):
        # Smoke test the contract: pre_execute must return the inbound
        # message. The wrong return-type annotation was a regression
        # caught earlier — pin it.
        from unittest.mock import MagicMock

        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "worker")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=1):
            mw = HeartbeatMiddleware(redis_url="redis://x")
            try:
                await mw.startup()
                msg = MagicMock()
                result = await mw.pre_execute(msg)
                assert result is msg
            finally:
                await mw.shutdown()

    async def test_post_execute_refreshes_key(
        self,
        fake_redis_factory,
        monkeypatch,
    ):
        from unittest.mock import MagicMock

        monkeypatch.setenv("TASKIQ_PROCESS_ROLE", "worker")
        with patch("downloader_bot.worker.healthcheck.os.getpid", return_value=1):
            mw = HeartbeatMiddleware(redis_url="redis://x")
            try:
                await mw.startup()
                # Force the key to expire so the refresh has an observable effect.
                await fake_redis_factory.delete(mw._key)
                assert await fake_redis_factory.get(mw._key) is None

                await mw.post_execute(MagicMock(), MagicMock())

                assert await fake_redis_factory.get(mw._key) == "1"
            finally:
                await mw.shutdown()


# --- CLI probe -------------------------------------------------------------


class TestCliProbe:
    async def test_returns_0_when_a_fresh_key_exists(self, fake_redis_factory):
        await fake_redis_factory.set("taskiq:heartbeat:worker:1", "1", ex=60)

        assert await _check("worker") == 0

    async def test_returns_1_when_no_matching_keys(self, fake_redis_factory):
        assert await _check("worker") == 1

    async def test_role_isolation_scheduler_key_does_not_satisfy_worker_probe(
        self,
        fake_redis_factory,
    ):
        # Scheduler heartbeat present but probe is asking for worker role:
        # must return 1, not get fooled by the wrong-role keyspace.
        await fake_redis_factory.set("taskiq:heartbeat:scheduler:1", "1", ex=60)

        assert await _check("worker") == 1
        assert await _check("scheduler") == 0

    async def test_returns_0_when_multiple_workers_present(
        self,
        fake_redis_factory,
    ):
        # Multi-worker deploy: any one fresh key satisfies the probe.
        await fake_redis_factory.set("taskiq:heartbeat:worker:1", "1", ex=60)
        await fake_redis_factory.set("taskiq:heartbeat:worker:2", "1", ex=60)
        await fake_redis_factory.set("taskiq:heartbeat:worker:3", "1", ex=60)

        assert await _check("worker") == 0
