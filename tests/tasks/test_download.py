"""Branch tests for ``download_channel_media`` — the Taskiq orchestrator.

Calls ``download_channel_media.original_func`` to bypass the Taskiq
broker plumbing while still going through ``cancellation_backend.cancellable``
(which has an explicit "no taskiq context → call original directly" branch,
so it's safe to invoke this way).

Resources that the runtime injects via ``TaskiqDepends`` providers are passed
in as plain kwargs here. The orchestrator's signature is the test surface;
no broker, no Redis, no Postgres — just mocks.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from downloader_bot.db.guild_settings import GuildSettings
from downloader_bot.download.zip_stream import AttachmentStreamError
from downloader_bot.storage.exceptions import UploadError
from downloader_bot.tasks.download import download_channel_media


def _messageable_channel():
    """Return a channel mock that passes ``isinstance(..., discord.abc.Messageable)``."""
    channel = MagicMock(spec=discord.abc.Messageable)
    return channel


async def _call(
    *,
    channel_id: int = 555,
    user_id: int = 42,
    guild_id: int | None = 12345,
    only_me: bool = False,
    context,
    progress,
    client,
    download_session,
    storage,
    settings_repo,
    redis,
):
    """Call through ``.original_func`` so we skip the broker but still hit
    ``cancellation_backend.cancellable``'s no-context fast path."""
    return await download_channel_media.original_func(
        channel_id=channel_id,
        user_id=user_id,
        guild_id=guild_id,
        only_me=only_me,
        context=context,
        progress=progress,
        client=client,
        download_session=download_session,
        storage=storage,
        settings_repo=settings_repo,
        redis=redis,
    )


@pytest.fixture
def progress():
    p = MagicMock()
    p.set_progress = AsyncMock()
    return p


@pytest.fixture
def ctx(task_context):
    return task_context("task-abc")


# --- Happy paths -----------------------------------------------------------


class TestHappyPathDm:
    async def test_uploads_streams_and_dms(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # No cached idempotency state — fresh task run.
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=0)
        mock_storage_backend.upload_and_sign = AsyncMock(
            return_value="https://example/signed?sas"
        )
        mock_discord_client.fetch_channel.return_value = _messageable_channel()

        # build_zip_stream is exercised in its own test module; here we just
        # need it to return something the upload mock can accept.
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(name="stream"),
        )
        dm_user = mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        result = await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=mock_settings_repo,
            redis=mock_redis,
        )

        assert result == {
            "url": "https://example/signed?sas",
            "delivery_mode": "dm",
        }
        mock_storage_backend.upload_and_sign.assert_awaited_once()
        dm_user.assert_awaited_once_with(
            mock_discord_client, 42, "https://example/signed?sas"
        )
        # Idempotency markers both written (archive_url + delivered).
        assert mock_redis.set.await_count == 2


class TestHappyPathChannelMode:
    async def test_posts_to_results_channel_when_configured(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        make_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        settings_repo = make_settings_repo(
            GuildSettings(
                guild_id=12345,
                delivery_mode="channel",
                results_channel_id=999,
            )
        )
        mock_storage_backend.upload_and_sign = AsyncMock(
            return_value="https://x/signed"
        )
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(name="stream"),
        )
        post_to_channel = mocker.patch(
            "downloader_bot.tasks.download.deliver.post_to_channel",
            new_callable=AsyncMock,
        )

        result = await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=settings_repo,
            redis=mock_redis,
        )

        assert result["delivery_mode"] == "channel"
        post_to_channel.assert_awaited_once_with(
            mock_discord_client,
            999,
            "https://x/signed",
            fallback_user_id=42,
        )

    async def test_channel_mode_without_results_channel_falls_back_to_dm(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        make_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # delivery_mode='channel' but no results_channel_id → orchestrator
        # branches to dm_user (preserves user-visibility).
        settings_repo = make_settings_repo(
            GuildSettings(
                guild_id=12345,
                delivery_mode="channel",
                results_channel_id=None,
            )
        )
        mock_storage_backend.upload_and_sign = AsyncMock(return_value="https://x")
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        dm_user = mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )
        post_to_channel = mocker.patch(
            "downloader_bot.tasks.download.deliver.post_to_channel",
            new_callable=AsyncMock,
        )

        await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=settings_repo,
            redis=mock_redis,
        )

        dm_user.assert_awaited_once()
        post_to_channel.assert_not_awaited()


# --- only_me override ------------------------------------------------------


class TestOnlyMeOverride:
    async def test_only_me_forces_dm_regardless_of_guild_setting(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        make_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # Even with delivery_mode='channel' configured, only_me=True
        # short-circuits to DM. The repo should NOT be consulted in this case.
        settings_repo = make_settings_repo(
            GuildSettings(
                guild_id=12345,
                delivery_mode="channel",
                results_channel_id=999,
            )
        )
        mock_storage_backend.upload_and_sign = AsyncMock(return_value="https://x")
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        dm_user = mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        result = await _call(
            only_me=True,
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=settings_repo,
            redis=mock_redis,
        )

        assert result["delivery_mode"] == "dm"
        settings_repo.get.assert_not_awaited()  # repo bypassed entirely
        dm_user.assert_awaited_once()

    async def test_guild_id_none_skips_repo_lookup(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # DM-channel invocation has no guild_id; the repo isn't queried.
        mock_storage_backend.upload_and_sign = AsyncMock(return_value="https://x")
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        await _call(
            guild_id=None,
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=mock_settings_repo,
            redis=mock_redis,
        )

        mock_settings_repo.get.assert_not_awaited()


# --- Idempotency / retries ------------------------------------------------


class TestIdempotency:
    async def test_skips_upload_when_archive_url_cached(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # Prior attempt wrote the URL; current attempt must reuse it.
        mock_redis.get = AsyncMock(return_value="https://prior/url")
        mock_redis.exists = AsyncMock(return_value=0)
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        dm_user = mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        result = await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=mock_settings_repo,
            redis=mock_redis,
        )

        assert result["url"] == "https://prior/url"
        mock_storage_backend.upload_and_sign.assert_not_awaited()
        # fetch_channel must also not be called — the whole upload phase is skipped.
        mock_discord_client.fetch_channel.assert_not_awaited()
        dm_user.assert_awaited_once()

    async def test_skips_delivery_when_delivered_marker_set(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        mock_redis.get = AsyncMock(return_value="https://prior/url")
        mock_redis.exists = AsyncMock(return_value=1)  # already delivered
        dm_user = mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )
        post_to_channel = mocker.patch(
            "downloader_bot.tasks.download.deliver.post_to_channel",
            new_callable=AsyncMock,
        )

        result = await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=mock_settings_repo,
            redis=mock_redis,
        )

        assert result["url"] == "https://prior/url"
        dm_user.assert_not_awaited()
        post_to_channel.assert_not_awaited()

    async def test_caches_archive_url_after_successful_upload(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=0)
        mock_storage_backend.upload_and_sign = AsyncMock(
            return_value="https://fresh/url"
        )
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=mock_settings_repo,
            redis=mock_redis,
        )

        # Two `.set` calls: cache_archive_url + mark_delivered.
        keys_written = [c.args[0] for c in mock_redis.set.await_args_list]
        assert "task:task-abc:archive_url" in keys_written
        assert "task:task-abc:delivered" in keys_written


# --- Failure paths --------------------------------------------------------


class TestErrorPaths:
    async def test_non_messageable_channel_raises_typeerror(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
    ):
        # fetch_channel returns something that's not Messageable (e.g. a
        # ForumChannel without send). Orchestrator raises before doing work.
        mock_redis.get = AsyncMock(return_value=None)
        not_messageable = MagicMock()
        # spec= None means isinstance(..., discord.abc.Messageable) is False
        mock_discord_client.fetch_channel.return_value = not_messageable

        with pytest.raises(TypeError, match="not messageable"):
            await _call(
                context=ctx,
                progress=progress,
                client=mock_discord_client,
                download_session=mock_aiohttp_session,
                storage=mock_storage_backend,
                settings_repo=mock_settings_repo,
                redis=mock_redis,
            )

    async def test_upload_failure_triggers_blob_cleanup_and_reraises(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        mock_redis.get = AsyncMock(return_value=None)
        mock_storage_backend.upload_and_sign = AsyncMock(
            side_effect=UploadError("azure down")
        )
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )

        with pytest.raises(UploadError, match="azure down"):
            await _call(
                context=ctx,
                progress=progress,
                client=mock_discord_client,
                download_session=mock_aiohttp_session,
                storage=mock_storage_backend,
                settings_repo=mock_settings_repo,
                redis=mock_redis,
            )

        # try/finally cleanup ran.
        mock_storage_backend.delete_blob.assert_awaited_once()
        # The cached archive_url was NOT written (failed upload doesn't poison
        # the cache; retry will try again).
        for call in mock_redis.set.await_args_list:
            assert call.args[0] != "task:task-abc:archive_url"

    async def test_cleanup_swallows_upload_error_from_delete_blob(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # Original upload fails; cleanup also fails. The cleanup failure
        # must be swallowed so the *original* exception propagates.
        mock_redis.get = AsyncMock(return_value=None)
        mock_storage_backend.upload_and_sign = AsyncMock(
            side_effect=UploadError("primary failure")
        )
        mock_storage_backend.delete_blob = AsyncMock(
            side_effect=UploadError("cleanup also failed")
        )
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )

        with pytest.raises(UploadError, match="primary failure"):
            await _call(
                context=ctx,
                progress=progress,
                client=mock_discord_client,
                download_session=mock_aiohttp_session,
                storage=mock_storage_backend,
                settings_repo=mock_settings_repo,
                redis=mock_redis,
            )

    async def test_attachment_stream_error_during_upload_triggers_cleanup(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        mock_redis.get = AsyncMock(return_value=None)
        mock_storage_backend.upload_and_sign = AsyncMock(
            side_effect=AttachmentStreamError("mid-stream failure")
        )
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )

        with pytest.raises(AttachmentStreamError):
            await _call(
                context=ctx,
                progress=progress,
                client=mock_discord_client,
                download_session=mock_aiohttp_session,
                storage=mock_storage_backend,
                settings_repo=mock_settings_repo,
                redis=mock_redis,
            )

        mock_storage_backend.delete_blob.assert_awaited_once()


# --- Settings flow-through -------------------------------------------------


class TestSettingsFlowThrough:
    async def test_retention_hours_drives_upload_ttl(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        make_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # Custom retention propagates to storage.upload_and_sign(ttl=...).
        settings_repo = make_settings_repo(
            GuildSettings(guild_id=12345, retention_hours=2)
        )
        mock_storage_backend.upload_and_sign = AsyncMock(return_value="https://x")
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=settings_repo,
            redis=mock_redis,
        )

        ttl = mock_storage_backend.upload_and_sign.await_args.kwargs["ttl"]
        assert ttl == timedelta(hours=2)

    async def test_allowed_media_types_drives_zip_filter(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        make_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        settings_repo = make_settings_repo(
            GuildSettings(
                guild_id=12345,
                allowed_media_types=["image/png", "video/mp4"],
            )
        )
        mock_storage_backend.upload_and_sign = AsyncMock(return_value="https://x")
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        build_zip_stream = mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=settings_repo,
            redis=mock_redis,
        )

        allowed = build_zip_stream.call_args.kwargs["allowed_types"]
        assert allowed == {"image/png", "video/mp4"}

    async def test_allowed_media_types_none_passes_none_through(
        self,
        mock_discord_client,
        mock_aiohttp_session,
        mock_redis,
        mock_settings_repo,
        mock_storage_backend,
        progress,
        ctx,
        mocker,
    ):
        # Default GuildSettings has allowed_media_types=None → no filter.
        mock_storage_backend.upload_and_sign = AsyncMock(return_value="https://x")
        mock_discord_client.fetch_channel.return_value = _messageable_channel()
        build_zip_stream = mocker.patch(
            "downloader_bot.tasks.download.zip_stream.build_zip_stream",
            return_value=MagicMock(),
        )
        mocker.patch(
            "downloader_bot.tasks.download.deliver.dm_user",
            new_callable=AsyncMock,
        )

        await _call(
            context=ctx,
            progress=progress,
            client=mock_discord_client,
            download_session=mock_aiohttp_session,
            storage=mock_storage_backend,
            settings_repo=mock_settings_repo,
            redis=mock_redis,
        )

        assert build_zip_stream.call_args.kwargs["allowed_types"] is None
