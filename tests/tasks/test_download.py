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


def _messageable_channel(channel_id: int = 555, name: str = "general"):
    """Return a channel mock that passes ``isinstance(..., discord.abc.Messageable)``.

    ``id`` and ``name`` are set explicitly because ``discord.abc.Messageable``
    doesn't define them on the ABC — with ``spec=Messageable`` the mock would
    otherwise raise ``AttributeError`` when the orchestrator calls
    ``_display_filename(channel)``.
    """
    channel = MagicMock(spec=discord.abc.Messageable)
    channel.id = channel_id
    channel.name = name
    return channel


async def _call(
    *,
    channel_id: int = 555,
    user_id: int = 42,
    guild_id: int | None = 12345,
    dm_me: bool = False,
    filters=None,
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
        dm_me=dm_me,
        context=context,
        progress=progress,
        client=client,
        download_session=download_session,
        storage=storage,
        settings_repo=settings_repo,
        redis=redis,
        filters=filters,
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


# --- dm_me override ------------------------------------------------------


class TestDmMeOverride:
    async def test_dm_me_forces_dm_regardless_of_guild_setting(
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
        # Even with delivery_mode='channel' configured, dm_me=True
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
            dm_me=True,
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

    async def test_allowed_media_types_drives_zip_matcher(
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
        # Guild policy reaches the zip stream as a callable matcher (built
        # by filters.resolve). We exercise the matcher directly rather
        # than reaching into resolve's internals — the cog/task contract
        # is "build_zip_stream receives a callable that respects the
        # guild policy".
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

        matcher = build_zip_stream.call_args.kwargs["matches"]
        png = MagicMock(content_type="image/png")
        jpeg = MagicMock(content_type="image/jpeg")
        msg = MagicMock()
        assert matcher(png, msg) is True
        assert matcher(jpeg, msg) is False

    async def test_no_filters_no_guild_policy_matcher_accepts_everything(
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
        # Default GuildSettings has allowed_media_types=None and no
        # per-invocation filters → matcher returns True for everything.
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

        matcher = build_zip_stream.call_args.kwargs["matches"]
        weird = MagicMock(content_type="application/x-weird")
        assert matcher(weird, MagicMock()) is True
        # Date bounds are absent when neither user nor guild constrains them.
        assert build_zip_stream.call_args.kwargs["before"] is None
        assert build_zip_stream.call_args.kwargs["after"] is None


# --- Per-invocation filters -----------------------------------------------


class TestFiltersFlowThrough:
    async def test_filters_payload_narrows_matcher_and_date_bounds(
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
        # Cog passes a filters dict; the matcher must reflect it and
        # `during` must resolve to a non-trivial (before, after) pair.
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
            filters={"category": "gif", "from_user_id": 42, "during": "today"},
        )

        kwargs = build_zip_stream.call_args.kwargs
        gif_from_42 = MagicMock(content_type="image/gif")
        msg_42 = MagicMock()
        msg_42.author = MagicMock()
        msg_42.author.id = 42
        msg_99 = MagicMock()
        msg_99.author = MagicMock()
        msg_99.author.id = 99
        png_from_42 = MagicMock(content_type="image/png")

        assert kwargs["matches"](gif_from_42, msg_42) is True
        assert kwargs["matches"](png_from_42, msg_42) is False
        assert kwargs["matches"](gif_from_42, msg_99) is False
        # `today` resolves to (start-of-day, now) — both sides set.
        assert kwargs["before"] is not None
        assert kwargs["after"] is not None
        assert kwargs["after"] <= kwargs["before"]

    async def test_filters_none_default_matches_today_behaviour(
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
        # Round-trip: when filters is omitted, behaviour should be the
        # same as before the feature shipped — no date bounds, matcher
        # only enforces the guild policy (which is None here, so it
        # accepts everything).
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

        kwargs = build_zip_stream.call_args.kwargs
        assert kwargs["before"] is None
        assert kwargs["after"] is None
        assert (
            kwargs["matches"](MagicMock(content_type="image/png"), MagicMock()) is True
        )
