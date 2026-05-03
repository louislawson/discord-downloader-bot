"""Unit tests for ``downloader_bot.embeds``.

Coverage targets the regressions hit during review:

* Each factory's colour / author / timestamp shape.
* Timestamp is UTC-aware (not local time).
* Timestamp is evaluated on each call, not at import time.
* ``media_download`` field order and labels (Images then Videos, not "Images"
  twice).
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


class TestMediaDownload:
    @pytest.fixture
    def embed(self):
        return embeds.media_download(
            signed_url="https://example.invalid/x.zip?token=abc",
            image_count=3,
            video_count=5,
            requester="alice#0001",
        )

    def test_field_names_and_order(self, embed):
        # A previous refactor mislabelled the Videos field as "Images"; pin
        # both labels and their order.
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Images", "3"),
            ("Videos", "5"),
        ]

    def test_description_contains_signed_url(self, embed):
        assert "https://example.invalid/x.zip?token=abc" in embed.description

    def test_footer_includes_requester(self, embed):
        assert embed.footer.text == "Requested by alice#0001"

    def test_inherits_success_colour_and_author(self, embed):
        assert embed.colour == discord.Color.green()
        assert embed.author.name == "Downloader Bot"
