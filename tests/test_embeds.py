"""Unit tests for ``downloader_bot.embeds``.

Coverage targets the regressions hit during review:

* Each factory's colour / author / timestamp shape.
* Timestamp is UTC-aware (not local time).
* Timestamp is evaluated on each call, not at import time.
* ``media_download`` description carries the signed URL and the footer
  mentions the requester.
"""

from datetime import UTC, datetime

import discord
import pytest

from downloader_bot import embeds


class TestSimpleFactories:
    """Shared behaviour for ``success`` / ``error`` / ``info``."""

    @pytest.mark.parametrize(
        "factory, expected_colour",
        [
            (embeds.success, discord.Color.green()),
            (embeds.error, discord.Color.red()),
            (embeds.info, discord.Color.blurple()),
        ],
    )
    def test_colour(self, factory, expected_colour):
        embed = factory(title="hi")
        assert embed.colour == expected_colour

    @pytest.mark.parametrize("factory", [embeds.success, embeds.error, embeds.info])
    def test_author_name(self, factory):
        embed = factory(title="hi")
        assert embed.author.name == "Downloader Bot"

    @pytest.mark.parametrize("factory", [embeds.success, embeds.error, embeds.info])
    def test_timestamp_is_utc_aware(self, factory):
        embed = factory(title="hi")
        assert embed.timestamp is not None
        assert embed.timestamp.tzinfo is UTC

    @pytest.mark.parametrize("factory", [embeds.success, embeds.error, embeds.info])
    def test_title_and_description_passthrough(self, factory):
        embed = factory(title="some title", description="some description")
        assert embed.title == "some title"
        assert embed.description == "some description"


class TestTimestampIsEvaluatedPerCall:
    def test_two_calls_get_distinct_timestamps(self, monkeypatch):
        # If ``datetime.now(...)`` were used as a default-argument value
        # (evaluated once at import), both embeds would share the same fixed
        # timestamp from process startup.
        ts1 = datetime(2026, 1, 1, tzinfo=UTC)
        ts2 = datetime(2026, 1, 2, tzinfo=UTC)
        values = iter([ts1, ts2])

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return next(values)

        monkeypatch.setattr(embeds, "datetime", FakeDatetime)

        first = embeds.success(title="first")
        second = embeds.success(title="second")

        assert first.timestamp == ts1
        assert second.timestamp == ts2


class TestNoAttachments:
    @pytest.fixture
    def embed(self):
        return embeds.no_attachments(requester_id=42)

    def test_inherits_error_colour_and_author(self, embed):
        assert embed.colour == discord.Color.red()
        assert embed.author.name == "Downloader Bot"

    def test_footer_mentions_requester(self, embed):
        assert "<@42>" in embed.footer.text

    def test_title_signals_empty_result(self, embed):
        assert embed.title == "No media found"


class TestMediaDownload:
    @pytest.fixture
    def embed(self):
        return embeds.media_download(
            signed_url="https://example.invalid/x.zip?token=abc",
            requester_id=42,
        )

    def test_description_contains_signed_url(self, embed):
        assert "https://example.invalid/x.zip?token=abc" in embed.description

    def test_footer_mentions_requester(self, embed):
        assert "<@42>" in embed.footer.text

    def test_inherits_success_colour_and_author(self, embed):
        assert embed.colour == discord.Color.green()
        assert embed.author.name == "Downloader Bot"
