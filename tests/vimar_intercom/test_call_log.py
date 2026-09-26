"""Tests for the local call log: rings, how they ended, missed calls.

Every panel, name and call ID here is invented.
"""

import pytest

from custom_components.vimar_intercom import call_log as cl

T0 = 1_700_000_000.0


def _ring(log, panel="55001", name="Front gate", ids=("abc@host",), now=T0):
    return log.ring(panel, name, ids, now)


def test_a_ring_is_logged_as_ringing_and_is_current():
    log = cl.CallLog()
    entry = _ring(log)
    assert log.ringing is entry
    assert entry.outcome == cl.OUTCOME_RINGING
    assert log.recent()[0]["outcome"] == "ringing"


def test_answering_here_ends_the_ring():
    log = cl.CallLog()
    _ring(log)
    entry = log.answered_here()
    assert entry.outcome == cl.OUTCOME_ANSWERED
    assert log.ringing is None
    assert log.missed_count == 0


def test_declining_ends_the_ring():
    log = cl.CallLog()
    _ring(log)
    assert log.declined().outcome == cl.OUTCOME_DECLINED
    assert log.ringing is None


def test_answered_elsewhere_matches_any_call_id_of_the_ring():
    log = cl.CallLog()
    _ring(log, ids=("sip-cid@host", "custom10ch"))
    assert log.answered_elsewhere("unrelated") is None
    entry = log.answered_elsewhere("custom10ch")
    assert entry.outcome == cl.OUTCOME_ANSWERED_ELSEWHERE
    assert log.ringing is None


def test_answered_elsewhere_without_an_id_means_the_current_ring():
    log = cl.CallLog()
    _ring(log)
    assert log.answered_elsewhere(None).outcome == cl.OUTCOME_ANSWERED_ELSEWHERE


def test_a_cancelled_ring_is_missed_only_after_the_grace_period():
    log = cl.CallLog()
    entry = _ring(log)
    ended = log.ring_ended()
    assert ended is entry and entry.outcome == cl.OUTCOME_UNANSWERED
    assert log.ringing is None
    assert log.missed_count == 0
    assert log.finalize(entry.id) is entry
    assert entry.outcome == cl.OUTCOME_MISSED
    assert log.missed_count == 1
    # A second finalize is a no-op.
    assert log.finalize(entry.id) is None
    assert log.missed_count == 1


def test_the_answered_notice_inside_the_grace_period_wins():
    log = cl.CallLog()
    entry = _ring(log)
    log.ring_ended()
    assert log.answered_elsewhere("abc@host") is entry
    assert log.finalize(entry.id) is None
    assert entry.outcome == cl.OUTCOME_ANSWERED_ELSEWHERE
    assert log.missed_count == 0


def test_the_units_missed_call_confirms_a_local_ring_once():
    log = cl.CallLog()
    entry = _ring(log)
    log.ring_ended()
    got, new = log.missed_call("55001", "Front gate", T0 + 20)
    assert got is entry and new is True
    assert log.missed_count == 1
    # The grace timer then finds nothing to do.
    assert log.finalize(entry.id) is None
    assert log.missed_count == 1


def test_a_missed_call_after_the_local_finalize_is_not_counted_twice():
    log = cl.CallLog()
    entry = _ring(log)
    log.ring_ended()
    log.finalize(entry.id)
    got, new = log.missed_call("55001", "Front gate", T0 + 30)
    assert got is entry and new is False
    assert log.missed_count == 1
    assert len(log.entries) == 1


def test_a_missed_call_with_no_local_ring_is_a_new_entry():
    log = cl.CallLog()
    _ring(log, panel="55002", name="Lobby")
    log.answered_here()
    got, new = log.missed_call("55001", "Front gate", T0 + 5)
    assert new is True
    assert got.panel == "55001" and got.outcome == cl.OUTCOME_MISSED
    assert len(log.entries) == 2
    assert log.entries[0] is got


def test_a_missed_call_far_from_any_ring_is_a_new_entry():
    log = cl.CallLog()
    _ring(log)
    log.ring_ended()
    _got, new = log.missed_call("55001", "Front gate", T0 + 3600)
    assert new is True
    assert len(log.entries) == 2


def test_a_new_ring_while_one_is_ringing_leaves_the_old_one_unanswered():
    log = cl.CallLog()
    first = _ring(log)
    second = _ring(log, panel="55002", ids=("other@host",), now=T0 + 2)
    assert log.ringing is second
    assert first.outcome == cl.OUTCOME_UNANSWERED


def test_clearing_resets_the_count_but_keeps_the_history():
    log = cl.CallLog()
    log.missed_call("55001", "Front gate", T0)
    log.clear_missed()
    assert log.missed_count == 0
    assert len(log.entries) == 1


def test_the_log_is_capped():
    log = cl.CallLog()
    for i in range(cl.MAX_ENTRIES + 5):
        log.missed_call("55001", "Front gate", T0 + i * 1000)
    assert len(log.entries) == cl.MAX_ENTRIES
    assert log.missed_count == cl.MAX_ENTRIES + 5


def test_recent_is_newest_first_with_iso_times_and_no_call_ids():
    log = cl.CallLog()
    _ring(log, now=T0)
    log.answered_here()
    log.missed_call("55002", "Lobby", T0 + 600)
    recent = log.recent()
    assert [r["panel"] for r in recent] == ["55002", "55001"]
    assert recent[0]["time"].startswith("2023-11-14T")
    assert set(recent[0]) == {"time", "panel", "name", "outcome"}


def test_a_round_trip_keeps_the_log_and_the_count():
    log = cl.CallLog()
    _ring(log)
    log.answered_here()
    log.missed_call("55002", "Lobby", T0 + 60)
    again = cl.CallLog.from_dict(log.to_dict())
    assert again.missed_count == 1
    assert [e.outcome for e in again.entries] == ["missed", "answered"]
    # New entries do not reuse an ID.
    assert again.missed_call("55001", "x", T0 + 9999)[0].id > max(
        e.id for e in log.entries)


def test_a_ring_interrupted_by_a_restart_is_restored_as_unanswered():
    log = cl.CallLog()
    _ring(log)
    again = cl.CallLog.from_dict(log.to_dict())
    assert again.ringing is None
    assert again.entries[0].outcome == cl.OUTCOME_UNANSWERED


@pytest.mark.parametrize("data", [
    None, [], {"entries": "x"}, {"entries": [{"time": "x"}]},
    {"missed_count": -3}, {"entries": [{"time": 1, "panel": 5}]},
])
def test_a_damaged_store_gives_an_empty_or_partial_log_not_an_error(data):
    log = cl.CallLog.from_dict(data)
    assert log.missed_count >= 0
    assert all(isinstance(e.panel, str) for e in log.entries)


@pytest.mark.parametrize(("raw", "expected"), [
    (1_700_000_000, 1_700_000_000.0),
    (1_700_000_000_123, 1_700_000_000.123),
    (None, None), (-5, None),
])
def test_epoch_seconds_reads_seconds_or_milliseconds(raw, expected):
    assert cl.epoch_seconds(raw) == expected
