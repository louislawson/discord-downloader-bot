"""Branch tests for ``download_channel_media`` — the four-phase ARQ pipeline.

Mocks ``deliver`` and ``ContainerRepository`` at the import site in
``downloader_bot.worker.jobs``. ``ZipFile`` and ``BytesIO`` are real so the
size-fallback branch is meaningful.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from arq.worker import Retry

from downloader_bot.storage.exceptions import (
    BlobUploadError,
    ContainerConfigError,
)
from downloader_bot.worker.jobs import download_channel_media


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


def _container_cm(repo):
    """Wrap ``repo`` as the async-context-manager that ``ContainerRepository()`` returns."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=repo)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _channel_with_messages(messages, async_iter, name="testchannel"):
    """Build a channel mock whose ``history(...)`` yields ``messages``."""
    channel = MagicMock()
    channel.name = name
    channel.history.return_value = async_iter(messages)
    return channel


@pytest.fixture
def mock_deliver(mocker):
    """Patch ``deliver`` at the jobs-module import site."""
    return mocker.patch(
        "downloader_bot.worker.jobs.deliver",
        new_callable=AsyncMock,
    )


@pytest.fixture
def mock_resolve_guild(mocker):
    """Patch ``_resolve_guild`` to return a tier-0 guild (8 MB upload limit)."""
    return mocker.patch(
        "downloader_bot.worker.jobs._resolve_guild",
        new_callable=AsyncMock,
        return_value=MagicMock(premium_tier=0),
    )


class TestHappyPath:
    async def test_uploads_and_delivers_sas_url_with_dev_url_rewrite(
        self,
        arq_ctx,
        mocker,
        async_iter,
        make_attachment,
        make_message,
        mock_deliver,
        mock_resolve_guild,
    ):
        img = make_attachment(content_type="image/png", filename="a.png")
        vid = make_attachment(content_type="video/mp4", filename="b.mp4")
        msg = make_message(message_id=1, attachments=[img, vid])
        channel = _channel_with_messages([msg], async_iter)
        arq_ctx["discord_client"].fetch_channel.return_value = channel

        repo = MagicMock()
        repo.create = AsyncMock(
            return_value=MagicMock(
                blob_name="testchannel-media.zip",
                url="http://azurite:10000/media/testchannel-media.zip",
            ),
        )
        repo.sas_url = AsyncMock(
            return_value="http://azurite:10000/media/testchannel-media.zip?sas",
        )
        mocker.patch(
            "downloader_bot.worker.jobs.ContainerRepository",
            return_value=_container_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result["ok"] is True
        # Dev URL rewrite happened: ST_INT_URL → ST_EXT_URL.
        assert result["sas_url"].startswith("http://localhost:10000/")
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Channel Media Download"


class TestEmptyChannel:
    async def test_no_allowed_media_returns_empty_reason(
        self,
        arq_ctx,
        async_iter,
        mock_deliver,
        mock_resolve_guild,
    ):
        channel = _channel_with_messages([], async_iter)
        arq_ctx["discord_client"].fetch_channel.return_value = channel

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "empty"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "No media found"


class TestForbiddenHistory:
    async def test_forbidden_during_history_returns_forbidden_reason(
        self,
        arq_ctx,
        forbidden_factory,
        mock_deliver,
        mock_resolve_guild,
    ):
        forbidden = forbidden_factory()

        class _RaisingIter:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise forbidden

        channel = MagicMock()
        channel.name = "locked"
        channel.history.return_value = _RaisingIter()
        arq_ctx["discord_client"].fetch_channel.return_value = channel

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "forbidden"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Missing permissions"


class TestStorageErrors:
    async def test_container_config_error_returns_config_reason(
        self,
        arq_ctx,
        mocker,
        async_iter,
        make_attachment,
        make_message,
        mock_deliver,
        mock_resolve_guild,
    ):
        img = make_attachment()
        msg = make_message(attachments=[img])
        channel = _channel_with_messages([msg], async_iter)
        arq_ctx["discord_client"].fetch_channel.return_value = channel

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=ContainerConfigError("missing"))
        cm.__aexit__ = AsyncMock(return_value=False)
        mocker.patch(
            "downloader_bot.worker.jobs.ContainerRepository",
            return_value=cm,
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "config"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Storage misconfigured"

    async def test_blob_upload_error_under_limit_falls_back_to_attachment(
        self,
        arq_ctx,
        mocker,
        async_iter,
        make_attachment,
        make_message,
        mock_deliver,
        mock_resolve_guild,
    ):
        img = make_attachment(payload=b"x" * 100)
        msg = make_message(attachments=[img])
        channel = _channel_with_messages([msg], async_iter)
        arq_ctx["discord_client"].fetch_channel.return_value = channel

        repo = MagicMock()
        repo.create = AsyncMock(side_effect=BlobUploadError("azure down"))
        mocker.patch(
            "downloader_bot.worker.jobs.ContainerRepository",
            return_value=_container_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": True, "fallback": "attachment"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title.startswith(
            "Channel Media Download (direct attachment)"
        )
        assert delivered.attachment is not None
        _buffer, filename = delivered.attachment
        assert filename == "testchannel-media.zip"

    async def test_blob_upload_error_over_limit_returns_too_large_reason(
        self,
        arq_ctx,
        mocker,
        async_iter,
        make_attachment,
        make_message,
        mock_deliver,
        mock_resolve_guild,
    ):
        # Uncompressible random bytes — 9 MB stays > 8 MB after deflate.
        big = make_attachment(payload=os.urandom(9 * 1024 * 1024))
        msg = make_message(attachments=[big])
        channel = _channel_with_messages([msg], async_iter)
        arq_ctx["discord_client"].fetch_channel.return_value = channel

        repo = MagicMock()
        repo.create = AsyncMock(side_effect=BlobUploadError("azure down"))
        mocker.patch(
            "downloader_bot.worker.jobs.ContainerRepository",
            return_value=_container_cm(repo),
        )

        result = await download_channel_media(arq_ctx, _payload())

        assert result == {"ok": False, "reason": "upload_failed_too_large"}
        mock_deliver.assert_awaited_once()
        delivered = mock_deliver.await_args.args[-1]
        assert delivered.embed.title == "Upload failed"


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
