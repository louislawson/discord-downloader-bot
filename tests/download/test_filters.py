"""Tests for ``downloader_bot.download.filters``.

Covers:

1. ``parse_duration`` — grammar accept/reject and the zero-magnitude guard.
2. ``resolve_named_period`` — each named period maps to the right
   ``(after, before)`` window (parametrised with a frozen ``now``).
3. ``CATEGORY_PREDICATES`` — the ``gif`` vs ``image`` split-out, and the
   ``other`` catch-all is exactly the negation of the union.
4. ``resolve`` — intersects user-supplied category with the guild's
   ``allowed_media_types``; composes the matcher with the
   ``from_user_id`` constraint; mutual exclusion of ``during`` vs
   ``before`` / ``after`` is enforced by the cog, but ``resolve``
   itself silently prefers ``during`` when both are present (the cog
   reject path is its own test in the cog suite).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from downloader_bot.db.guild_settings import GuildSettings
from downloader_bot.download.filters import (
    CATEGORY_PREDICATES,
    NAMED_PERIODS,
    FilterParseError,
    parse_duration,
    resolve,
    resolve_named_period,
)


def _attachment(content_type: str):
    att = MagicMock()
    att.content_type = content_type
    return att


def _message(*, author_id: int = 1):
    msg = MagicMock()
    msg.author = MagicMock()
    msg.author.id = author_id
    return msg


# --- parse_duration --------------------------------------------------------


class TestParseDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("30m", timedelta(minutes=30)),
            ("2h", timedelta(hours=2)),
            ("7d", timedelta(days=7)),
            ("3w", timedelta(weeks=3)),
            ("  7d  ", timedelta(days=7)),  # whitespace tolerated
        ],
    )
    def test_happy_path(self, value, expected):
        assert parse_duration(value) == expected

    @pytest.mark.parametrize(
        "value",
        ["", "7", "d", "7y", "-1d", "1.5h", "abc", "7 d"],
    )
    def test_rejects_bad_input(self, value):
        with pytest.raises(FilterParseError):
            parse_duration(value)

    def test_rejects_zero_magnitude(self):
        # `0d` parses syntactically but is semantically meaningless and
        # would collapse the date window — reject explicitly.
        with pytest.raises(FilterParseError, match="greater than zero"):
            parse_duration("0d")


# --- resolve_named_period --------------------------------------------------


class TestResolveNamedPeriod:
    # Anchor mid-week so week/month/year boundaries are non-trivially
    # different from `now`. Wednesday 2026-05-13 14:25:30 UTC.
    NOW = datetime(2026, 5, 13, 14, 25, 30, tzinfo=UTC)

    def test_today(self):
        after, before = resolve_named_period("today", self.NOW)
        assert after == datetime(2026, 5, 13, tzinfo=UTC)
        assert before == self.NOW

    def test_yesterday(self):
        after, before = resolve_named_period("yesterday", self.NOW)
        assert after == datetime(2026, 5, 12, tzinfo=UTC)
        assert before == datetime(2026, 5, 13, tzinfo=UTC)

    def test_this_week(self):
        # NOW is Wednesday → ISO week starts Monday 2026-05-11.
        after, before = resolve_named_period("this-week", self.NOW)
        assert after == datetime(2026, 5, 11, tzinfo=UTC)
        assert before == self.NOW

    def test_last_week(self):
        after, before = resolve_named_period("last-week", self.NOW)
        assert after == datetime(2026, 5, 4, tzinfo=UTC)
        assert before == datetime(2026, 5, 11, tzinfo=UTC)

    def test_this_month(self):
        after, before = resolve_named_period("this-month", self.NOW)
        assert after == datetime(2026, 5, 1, tzinfo=UTC)
        assert before == self.NOW

    def test_last_month(self):
        after, before = resolve_named_period("last-month", self.NOW)
        assert after == datetime(2026, 4, 1, tzinfo=UTC)
        assert before == datetime(2026, 5, 1, tzinfo=UTC)

    def test_last_month_across_january_boundary(self):
        # January's "last month" must land in the previous year.
        now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
        after, before = resolve_named_period("last-month", now)
        assert after == datetime(2025, 12, 1, tzinfo=UTC)
        assert before == datetime(2026, 1, 1, tzinfo=UTC)

    def test_this_year(self):
        after, before = resolve_named_period("this-year", self.NOW)
        assert after == datetime(2026, 1, 1, tzinfo=UTC)
        assert before == self.NOW

    def test_last_year(self):
        after, before = resolve_named_period("last-year", self.NOW)
        assert after == datetime(2025, 1, 1, tzinfo=UTC)
        assert before == datetime(2026, 1, 1, tzinfo=UTC)

    def test_rejects_unknown_period(self):
        with pytest.raises(FilterParseError, match="Unknown named period"):
            resolve_named_period("decade", self.NOW)

    def test_all_named_periods_resolve(self):
        # Sanity-check: every advertised period name is actually handled.
        for name in NAMED_PERIODS:
            after, before = resolve_named_period(name, self.NOW)
            assert after <= before


# --- CATEGORY_PREDICATES ---------------------------------------------------


class TestCategoryPredicates:
    @pytest.mark.parametrize(
        ("category", "mime", "expected"),
        [
            # image/* matches image, except gif which lives in its own bucket.
            ("image", "image/png", True),
            ("image", "image/jpeg", True),
            ("image", "image/webp", True),
            ("image", "image/gif", False),
            ("image", "video/mp4", False),
            ("image", "text/plain", False),
            # gif is its own bucket — only image/gif.
            ("gif", "image/gif", True),
            ("gif", "image/png", False),
            ("gif", "video/gif", False),
            # video/* is unsplit.
            ("video", "video/mp4", True),
            ("video", "video/webm", True),
            ("video", "image/png", False),
            # audio/* is unsplit.
            ("audio", "audio/mpeg", True),
            ("audio", "audio/ogg", True),
            ("audio", "image/png", False),
            # other = negation of the union.
            ("other", "text/plain", True),
            ("other", "application/pdf", True),
            ("other", "image/png", False),
            ("other", "image/gif", False),
            ("other", "video/mp4", False),
            ("other", "audio/mpeg", False),
        ],
    )
    def test_predicate(self, category, mime, expected):
        assert CATEGORY_PREDICATES[category](mime) is expected

    def test_other_is_exact_negation_of_union(self):
        # If a mime matches one of the named four, `other` must reject it.
        # If it matches none, `other` must accept it.
        samples = [
            "image/png",
            "image/gif",
            "image/webp",
            "video/mp4",
            "audio/ogg",
            "text/plain",
            "application/zip",
            "",
        ]
        for mime in samples:
            in_named = any(
                CATEGORY_PREDICATES[name](mime)
                for name in ("image", "video", "audio", "gif")
            )
            assert CATEGORY_PREDICATES["other"](mime) is not in_named


# --- resolve ---------------------------------------------------------------


class TestResolveMatcher:
    NOW = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

    def test_no_filters_no_guild_policy_matches_everything(self):
        resolved = resolve(None, GuildSettings(guild_id=1), now=self.NOW)
        # Empty filter dict + no allowed_media_types → every attachment
        # passes regardless of content type or author.
        assert resolved.matches(_attachment("video/x-matroska"), _message()) is True
        assert resolved.matches(_attachment("text/plain"), _message()) is True
        assert resolved.before is None
        assert resolved.after is None

    def test_category_intersects_with_guild_allowed_types(self):
        # Guild policy allows image/png + video/mp4. User asks for category
        # `image`. Intersection: only image/png passes (image/jpeg, even
        # though it's an image, is outside the guild policy).
        guild = GuildSettings(
            guild_id=1,
            allowed_media_types=["image/png", "video/mp4"],
        )
        resolved = resolve({"category": "image"}, guild, now=self.NOW)
        assert resolved.matches(_attachment("image/png"), _message()) is True
        assert resolved.matches(_attachment("image/jpeg"), _message()) is False
        assert resolved.matches(_attachment("video/mp4"), _message()) is False

    def test_guild_policy_alone_filters_without_user_category(self):
        guild = GuildSettings(guild_id=1, allowed_media_types=["image/png"])
        resolved = resolve(None, guild, now=self.NOW)
        assert resolved.matches(_attachment("image/png"), _message()) is True
        assert resolved.matches(_attachment("video/mp4"), _message()) is False

    def test_from_user_id_filters_on_author(self):
        resolved = resolve(
            {"from_user_id": 42},
            GuildSettings(guild_id=1),
            now=self.NOW,
        )
        assert resolved.matches(_attachment("image/png"), _message(author_id=42))
        assert not resolved.matches(_attachment("image/png"), _message(author_id=99))

    def test_category_and_from_user_compose(self):
        resolved = resolve(
            {"category": "gif", "from_user_id": 42},
            GuildSettings(guild_id=1),
            now=self.NOW,
        )
        # Both must hold.
        assert resolved.matches(_attachment("image/gif"), _message(author_id=42))
        assert not resolved.matches(_attachment("image/gif"), _message(author_id=99))
        assert not resolved.matches(_attachment("image/png"), _message(author_id=42))

    def test_content_type_with_parameters_is_normalised(self):
        # "image/png; charset=utf-8" should match the image category.
        resolved = resolve(
            {"category": "image"}, GuildSettings(guild_id=1), now=self.NOW
        )
        assert resolved.matches(
            _attachment("image/png; charset=utf-8"),
            _message(),
        )

    def test_missing_content_type_falls_into_other(self):
        # Discord attachments occasionally come back with content_type=None.
        # Such items should not match a positive category like `image`,
        # but should match `other` (the catch-all).
        resolved_image = resolve(
            {"category": "image"}, GuildSettings(guild_id=1), now=self.NOW
        )
        resolved_other = resolve(
            {"category": "other"}, GuildSettings(guild_id=1), now=self.NOW
        )
        att = MagicMock()
        att.content_type = None
        assert resolved_image.matches(att, _message()) is False
        assert resolved_other.matches(att, _message()) is True


class TestResolveDateBounds:
    NOW = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

    def test_relative_before_and_after_compose(self):
        resolved = resolve(
            {"before": "1d", "after": "7d"},
            GuildSettings(guild_id=1),
            now=self.NOW,
        )
        # `before:1d` → messages older than 1d ago.
        assert resolved.before == self.NOW - timedelta(days=1)
        # `after:7d` → messages newer than 7d ago.
        assert resolved.after == self.NOW - timedelta(days=7)

    def test_during_overrides_relative_bounds(self):
        # The cog enforces mutual exclusion; if both are passed,
        # `resolve` uses `during` and ignores `before`/`after`. The
        # alternative would be silently mixing them, which is worse.
        resolved = resolve(
            {"during": "this-month", "before": "1d", "after": "30d"},
            GuildSettings(guild_id=1),
            now=self.NOW,
        )
        assert resolved.after == datetime(2026, 5, 1, tzinfo=UTC)
        assert resolved.before == self.NOW

    def test_during_alone_sets_both_bounds(self):
        resolved = resolve(
            {"during": "today"},
            GuildSettings(guild_id=1),
            now=self.NOW,
        )
        assert resolved.after == datetime(2026, 5, 13, tzinfo=UTC)
        assert resolved.before == self.NOW

    def test_bad_duration_in_filter_raises_filter_parse_error(self):
        # The cog validates upfront, but `resolve` should be defensive too.
        with pytest.raises(FilterParseError):
            resolve(
                {"after": "nope"},
                GuildSettings(guild_id=1),
                now=self.NOW,
            )
