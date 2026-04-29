"""Bot presence statuses and the picker used by ``DiscordBot.status_task``."""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from typing import Final

# Rendered as "Playing X" by Discord. Keep entries short — Discord truncates
# long activity strings in the member list.
STATUSES: Final[tuple[str, ...]] = (
    "Packaging memories… please hold.",
    "I collect. I compress. I disappear.",
    "Zipping… eventually.",
    "Downloading the internet (locally).",
    "I only queue jobs. Someone else suffers.",
    "Delegating responsibility since 2024.",
    "Queued. Detached. Emotionless.",
    "Uploading to The Cloud™.",
    "Now stored somewhere expensive.",
    "Converting memes into Cloud liabilities.",
    "Paid for by unused enterprise credits.",
    "If this fails, blame the Cloud™.",
    "You post it, I take it.",
    "This channel looked better as a zip file.",
    "Archiving things you'll never open.",
    "Because scrolling is for cowards.",
    "Yes, even that image.",
    "Working longer than Discord allows.",
    "Still going. Don't worry. Or do.",
    "Good things come to those who wait.",
    "15 minutes wasn't enough.",
    "Reducing many files into one.",
    "I make files hug each other.",
    "Compressing chaos.",
    "One zip to hold them all.",
    "If the cloud fails, I carry it myself.",
    "Delivering by hand. Digitally.",
)


def cycle_random(statuses: Sequence[str]) -> Iterator[str]:
    """Yield statuses uniformly at random, never the same one twice in a row.

    The generator owns the previous-pick state so callers can simply call
    ``next(picker)`` each tick. With a single-element ``statuses`` it yields
    that element forever — it is the only valid pick, so the no-repeat rule
    is relaxed.
    """
    if not statuses:
        raise ValueError("statuses must not be empty")
    previous: str | None = None
    while True:
        choice = random.choice(statuses)
        if choice == previous and len(statuses) > 1:
            continue
        previous = choice
        yield choice
