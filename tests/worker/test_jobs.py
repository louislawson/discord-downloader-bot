"""Branch tests for ``download_channel_media`` — the streaming ARQ pipeline.

Mocks ``deliver``, ``get_storage_backend``, and ``build_zip_stream`` at the
import site in ``downloader_bot.worker.jobs``. The streaming pipeline itself
is tested in ``test_zip_stream.py`` — these tests focus on the orchestration
around it (error branches, empty-channel cleanup, success delivery).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from arq.worker import Retry

from downloader_bot.storage.exceptions import (
    SignedUrlError,
    StorageConfigError,
    UploadError,
)
from downloader_bot.worker.jobs import download_channel_media
from downloader_bot.worker.zip_stream import (
    AttachmentStreamError,
    Counters,
    ZipStreamResult,
)


def _payload(**overrides):
    base = {
        "job_id": "job-abc",
        "channel_id": 555,
        "guild_id": 12345,
        "requester_id": 42,
        "requester_tag": "user#0001",
        "only_me": False,
        "allowed_media_types": ["image/png", "video/mp4"],
    }
    base.update(overrides)
    return base


def _backend_cm(repo):
    """Wrap ``repo`` as the async-context-manager that ``get_storage_backend()`` returns."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=repo)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _channel_mock(name="testchannel"):
    """Channel mock; the streaming pipeline doesn't read .history() in these
    tests because ``build_zip_stream`` is patched out."""
    channel = MagicMock()
    channel.name = name
    return channel


def _stream_result(images=1, videos=0, chunks=(b"abc",), raises=None):
    """Build a ``ZipStreamResult`` with predetermined counters and iterable.

    The iterable yields ``chunks`` then stops, unless ``raises`` is given —
    in which case it raises that exception on first ``__anext__`` (simulating
    a lazy failure during stream consumption).
    """
    counters = Counters(images=images, videos=videos)

    async def _iter():
        if raises is not None:
            raise raises
        for c in chunks:
            yield c

    return ZipStreamResult(iterable=_iter(), counters=counters)


def _draining_upload(return_url="https://example/signed?sas", raises=None):
    """Build an ``upload_and_sign`` side_effect that drains its data argument.

    Lazy errors raised by the iterable only fire if the consumer actually
    iterates — a plain ``AsyncMock(return_value=...)`` would skip iteration
    and the error would never surface.
    """

    async def _impl(*, name, data, overwrite=True):
        async for _ in data:
            pass
        if raises is not None:
            raise raises
        return return_url

    return _impl


@pytest.fixture
def mock_deliver(mocker):
    """Patch ``deliver`` at the jobs-module import site."""
    return mocker.patch(
        "downloader_bot.worker.jobs.deliver",
        new_callable=AsyncMock,
    )


@pytest.fixture
def patch_build_zip_stream(mocker):
    """Factory: patch ``build_zip_stream`` to return a stub ``ZipStreamResult``."""

    def _patch(result):
        return mocker.patch(
            "downloader_bot.worker.jobs.build_zip_stream",
            return_value=result,
        )

    return _patch


# --- Happy path -------------------------------------------------------------


class TestHappyPath:
    async def test_uploads_and_delivers_sas_url(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result(images=2, videos=1))

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(
            side_effect=_draining_upload(
                return_url="http://localhost:10000/media/testchannel-media.zip?sas",
            )
        )
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result["ok"] is True
        assert result["sas_url"].startswith("http://localhost:10000/")
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Channel Media Download"
        # Counter values flow through to the success embed.
        assert any(f.value == "2" for f in delivered.embed.fields)
        assert any(f.value == "1" for f in delivered.embed.fields)


# --- Empty channel ----------------------------------------------------------


class TestEmptyChannel:
    async def test_empty_channel_best_effort_deletes_orphan_zip(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result(images=0, videos=0, chunks=()))

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(side_effect=_draining_upload())
        repo.delete_blob = AsyncMock()
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "empty"}
        repo.delete_blob.assert_awaited_once_with("testchannel-media.zip")
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "No media found"

    async def test_empty_channel_swallows_delete_failure(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result(images=0, videos=0, chunks=()))

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(side_effect=_draining_upload())
        repo.delete_blob = AsyncMock(side_effect=UploadError("azure refused delete"))
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        # Function must complete normally — the failed delete is swallowed
        # and the user still gets the "No media found" embed.
        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "empty"}
        repo.delete_blob.assert_awaited_once()
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "No media found"


# --- Discord errors during the stream --------------------------------------


class TestForbiddenHistory:
    async def test_forbidden_during_history_returns_forbidden_reason(
        self,
        arq_ctx,
        mocker,
        forbidden_factory,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result(raises=forbidden_factory()))

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(side_effect=_draining_upload())
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "forbidden"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Missing permissions"


class TestDiscordHttpDuringStream:
    async def test_http_exception_during_history_returns_discord_http_reason(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        import discord

        # Build a non-Forbidden HTTPException (e.g. 500 from history walk).
        response = MagicMock(status=500, reason="Internal Server Error")
        http_exc = discord.HTTPException(response, "boom")

        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result(raises=http_exc))

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(side_effect=_draining_upload())
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "discord_http"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Discord error"


class TestAttachmentStreamError:
    async def test_mid_stream_failure_returns_attachment_stream_reason(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        patch_build_zip_stream(
            _stream_result(raises=AttachmentStreamError("connection reset"))
        )
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(side_effect=_draining_upload())
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "attachment_stream"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Discord error"


# --- Storage errors ---------------------------------------------------------


class TestStorageErrors:
    async def test_storage_config_error_returns_config_reason(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result())

        # StorageConfigError is raised by get_storage_backend() itself,
        # before the async-with even tries to enter.
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            side_effect=StorageConfigError("missing"),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "config"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Storage misconfigured"

    async def test_upload_error_returns_upload_failed_reason(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result())

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(
            side_effect=_draining_upload(raises=UploadError("azure down"))
        )
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "upload_failed"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Upload failed"

    async def test_signed_url_error_returns_upload_failed_reason(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
        patch_build_zip_stream,
    ):
        arq_ctx["discord_client"].fetch_channel.return_value = _channel_mock()
        patch_build_zip_stream(_stream_result())

        repo = MagicMock()
        repo.upload_and_sign = AsyncMock(
            side_effect=_draining_upload(raises=SignedUrlError("no key"))
        )
        mocker.patch(
            "downloader_bot.worker.jobs.get_storage_backend",
            return_value=_backend_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "upload_failed"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Upload failed"


# --- Outer wrapper ----------------------------------------------------------


class TestOuterWrapper:
    async def test_runtime_error_is_delivered_and_reraised(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
    ):
        mocker.patch(
            "downloader_bot.worker.jobs._run_download_channel_media",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        )

        with pytest.raises(RuntimeError, match="boom"):
            await download_channel_media(arq_ctx, _payload())

        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Download failed"

    async def test_retry_is_reraised_without_delivering(
        self,
        arq_ctx,
        mocker,
        mock_deliver,
    ):
        mocker.patch(
            "downloader_bot.worker.jobs._run_download_channel_media",
            new_callable=AsyncMock,
            side_effect=Retry(defer=1),
        )

        with pytest.raises(Retry):
            await download_channel_media(arq_ctx, _payload())

        mock_deliver.assert_not_awaited()
