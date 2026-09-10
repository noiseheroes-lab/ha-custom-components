"""Reconnect delay schedule.

Exponential backoff with a ceiling and proportional jitter, so a fleet
of installations recovering from the same provider outage does not
reconnect in lockstep. There is deliberately no attempt limit: the SIP
connection is the whole integration, and giving up means the doorbell
stops working until Home Assistant restarts.
"""

from __future__ import annotations

import random
from collections.abc import Callable

MAX_DOUBLINGS = 32


def reconnect_delay(
    attempt: int,
    *,
    base: float = 2.0,
    ceiling: float = 60.0,
    jitter_ratio: float = 0.25,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before reconnect attempt number `attempt`.

    `attempt` is 1 for the first retry. The nominal delay doubles each
    attempt up to `ceiling`, then jitter of +/- `jitter_ratio` is applied.
    """
    steps = min(max(attempt, 1) - 1, MAX_DOUBLINGS)
    nominal = min(base * (2 ** steps), ceiling)
    if not jitter_ratio:
        return nominal
    factor = 1.0 - jitter_ratio + 2.0 * jitter_ratio * rand()
    return nominal * factor
