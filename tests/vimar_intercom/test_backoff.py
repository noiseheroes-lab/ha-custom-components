"""Tests for the reconnect delay schedule."""

import pytest

from custom_components.vimar_intercom.backoff import reconnect_delay

NO_JITTER = {"jitter_ratio": 0.0}


def test_first_attempt_waits_the_base_delay():
    assert reconnect_delay(1, **NO_JITTER) == 2.0


def test_delay_doubles_each_attempt():
    delays = [reconnect_delay(n, **NO_JITTER) for n in range(1, 6)]
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_delay_is_capped_at_the_ceiling():
    assert reconnect_delay(6, **NO_JITTER) == 60.0
    assert reconnect_delay(100, **NO_JITTER) == 60.0


def test_delay_never_overflows_for_large_attempts():
    assert reconnect_delay(10_000, **NO_JITTER) == 60.0


def test_attempts_below_one_are_treated_as_the_first():
    assert reconnect_delay(0, **NO_JITTER) == 2.0
    assert reconnect_delay(-5, **NO_JITTER) == 2.0


def test_jitter_centres_on_the_nominal_delay():
    assert reconnect_delay(3, rand=lambda: 0.5) == 8.0


@pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_jitter_stays_within_the_configured_band(value):
    delay = reconnect_delay(3, rand=lambda: value)
    assert 6.0 <= delay <= 10.0


def test_jitter_actually_varies():
    assert reconnect_delay(3, rand=lambda: 0.0) != reconnect_delay(3, rand=lambda: 1.0)


def test_ceiling_is_configurable():
    assert reconnect_delay(20, ceiling=10.0, **NO_JITTER) == 10.0
