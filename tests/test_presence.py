"""Unit tests for ``downloader_bot.presence.cycle_random``."""

from itertools import islice, pairwise

import pytest

from downloader_bot.presence import STATUSES, cycle_random


class TestCycleRandom:
    def test_yields_only_known_statuses(self):
        picker = cycle_random(STATUSES)
        sample = list(islice(picker, 200))

        assert all(value in STATUSES for value in sample)

    def test_never_repeats_consecutively(self):
        # A small pool maximises the chance of pulling the same value twice
        # in a row, which is exactly what the picker must prevent.
        pool = ("a", "b", "c")
        picker = cycle_random(pool)
        sample = list(islice(picker, 1000))

        assert all(a != b for a, b in pairwise(sample))

    def test_single_element_yields_forever(self):
        picker = cycle_random(("only",))

        assert list(islice(picker, 5)) == ["only"] * 5

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            # Generators only execute their body on first ``next``; calling
            # the function alone wouldn't trigger the guard.
            next(cycle_random([]))
