"""The local call log: who rang, and how each ring ended.

The official app keeps its call log on the phone. The indoor unit only
sends what keeps several devices in step — `MISSED_CALL` when a panel
rang and nobody answered, and `C;<call id>;ANSWERED` when some device
picked up — and each app records its own rings from the INVITEs it
receives. This module is that record for Home Assistant: one entry per
ring, and the missed-call count the sensor shows.

A ring ends in one of four ways:

- answered here — Home Assistant took the call;
- declined — Home Assistant refused it;
- answered elsewhere — the unit's `C;<id>;ANSWERED` named this ring's
  call, or the panel's CANCEL carried `Reason: SIP;cause=200`, which is
  what the SDK reads as "answered by others";
- missed — the unit said so (`MISSED_CALL`), or the panel cancelled the
  ring and nothing said it was answered within `UNANSWERED_GRACE`.

The grace period exists because the order is not guaranteed: the device
that answered sends its `C;<id>;ANSWERED` when its call connects, and
the proxy cancels the other devices' INVITEs at the same moment. Calling
a cancelled ring missed on the spot would count every call answered on
the indoor unit as missed here. A `MISSED_CALL` from the unit is matched
to the local ring it is about (same panel, close in time), so one missed
visitor is counted once whichever of the two arrives first.

Nothing here imports Home Assistant or reads the clock: the hub passes
the time in and runs the grace timer, and persists `to_dict()` with a
Store. The entries hold panel extensions and the installer's panel
names, never a call ID: those are kept in memory only, for matching.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# The log a user can see is the last twenty rings; the count of missed
# calls keeps counting past that until it is cleared.
MAX_ENTRIES = 20
# Seconds after a panel's CANCEL before an unanswered ring counts as
# missed. See the module docstring.
UNANSWERED_GRACE = 5.0
# How far apart, in seconds, a MISSED_CALL and a local ring may be and
# still be the same visitor. The unit's clock is not ours, and the ring
# itself lasts up to a minute.
MISSED_MATCH_WINDOW = 180.0

OUTCOME_RINGING = "ringing"
OUTCOME_UNANSWERED = "unanswered"
OUTCOME_ANSWERED = "answered"
OUTCOME_ANSWERED_ELSEWHERE = "answered_elsewhere"
OUTCOME_DECLINED = "declined"
OUTCOME_MISSED = "missed"
OUTCOMES = frozenset({
    OUTCOME_RINGING, OUTCOME_UNANSWERED, OUTCOME_ANSWERED,
    OUTCOME_ANSWERED_ELSEWHERE, OUTCOME_DECLINED, OUTCOME_MISSED,
})

# Above this a timestamp is taken to be in milliseconds: 10^11 seconds is
# the year 5138, 10^11 milliseconds is March 1973.
_MILLIS_THRESHOLD = 100_000_000_000
_MAX_TEXT = 64


def epoch_seconds(raw: int | float | None) -> float | None:
    """A Unix time from the unit, in seconds, whatever unit it came in.

    The SDK stores `TS` and `ORIGTIME` as longs without converting them,
    so the decompiled app does not show whether they count seconds or
    milliseconds. The magnitude does.
    """
    if raw is None or isinstance(raw, bool) or raw < 0:
        return None
    value = float(raw)
    return value / 1000.0 if value >= _MILLIS_THRESHOLD else value


def iso_time(seconds: float) -> str:
    """An epoch time as an ISO 8601 string in UTC."""
    return datetime.fromtimestamp(seconds, UTC).isoformat()


@dataclass
class CallEntry:
    """One ring, or one missed call the unit told us about."""

    id: int
    time: float
    panel: str
    name: str
    outcome: str
    # Every call ID the ring's INVITE carried, for matching the unit's
    # C;<id>;ANSWERED. In memory only.
    call_ids: tuple[str, ...] = field(default=(), repr=False)
    # True once a MISSED_CALL has been matched to this entry, so a second
    # one for the same visitor is not matched to it again.
    reported: bool = False

    def as_attribute(self) -> dict[str, str]:
        """What the missed-calls sensor shows for this entry."""
        return {"time": iso_time(self.time), "panel": self.panel,
                "name": self.name, "outcome": self.outcome}


class CallLog:
    """The rings of this installation, newest first, and the missed count."""

    def __init__(self, entries: Iterable[CallEntry] = (),
                 missed_count: int = 0, next_id: int = 1) -> None:
        """Start from a restored log, or an empty one."""
        self.entries: list[CallEntry] = list(entries)[:MAX_ENTRIES]
        self.missed_count = max(0, missed_count)
        self._next_id = max([next_id, *(e.id + 1 for e in self.entries)])
        self.ringing: CallEntry | None = None

    # ─── rings ───────────────────────────────────────────────────────

    def _add(self, entry: CallEntry) -> CallEntry:
        self.entries.insert(0, entry)
        del self.entries[MAX_ENTRIES:]
        return entry

    def _new_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def ring(self, panel: str, name: str, call_ids: Iterable[str],
             now: float) -> CallEntry:
        """A panel is ringing Home Assistant.

        A ring still current is left unanswered: this client has one
        pending INVITE, and a second one replaced the first.
        """
        if self.ringing is not None:
            self.ringing.outcome = OUTCOME_UNANSWERED
        entry = self._add(CallEntry(
            id=self._new_id(), time=now, panel=panel, name=name,
            outcome=OUTCOME_RINGING, call_ids=tuple(call_ids)))
        self.ringing = entry
        return entry

    def _end_ring(self, outcome: str) -> CallEntry | None:
        entry = self.ringing
        if entry is None:
            return None
        entry.outcome = outcome
        self.ringing = None
        return entry

    def answered_here(self) -> CallEntry | None:
        """Home Assistant answered the current ring."""
        return self._end_ring(OUTCOME_ANSWERED)

    def declined(self) -> CallEntry | None:
        """Home Assistant declined the current ring."""
        return self._end_ring(OUTCOME_DECLINED)

    def ring_ended(self) -> CallEntry | None:
        """The panel cancelled the current ring, reason unknown yet.

        The entry is left unanswered; the caller runs `finalize` for it
        after `UNANSWERED_GRACE`.
        """
        return self._end_ring(OUTCOME_UNANSWERED)

    def answered_elsewhere(self, call_id: str | None) -> CallEntry | None:
        """Another device answered: the current ring, or one just cancelled.

        With a call ID (the unit's C;<id>;ANSWERED) only a ring whose
        INVITE carried that ID matches; without one (a CANCEL with cause
        200) the current ring does. Returns the entry, or None when the
        notice was about a call this installation never rang for.
        """
        if call_id is None:
            return self._end_ring(OUTCOME_ANSWERED_ELSEWHERE)
        for entry in self.entries:
            if (call_id in entry.call_ids
                    and entry.outcome in (OUTCOME_RINGING, OUTCOME_UNANSWERED)):
                if entry is self.ringing:
                    self.ringing = None
                entry.outcome = OUTCOME_ANSWERED_ELSEWHERE
                return entry
        return None

    def finalize(self, entry_id: int) -> CallEntry | None:
        """The grace period is over: an entry still unanswered is missed.

        Returns the entry when it became missed just now, None otherwise
        (it was answered after all, or the unit already reported it).
        """
        for entry in self.entries:
            if entry.id == entry_id:
                if entry.outcome != OUTCOME_UNANSWERED:
                    return None
                entry.outcome = OUTCOME_MISSED
                self.missed_count += 1
                return entry
        return None

    def missed_call(self, panel: str, name: str,
                    when: float) -> tuple[CallEntry, bool]:
        """The unit reported a missed call from `panel` at `when`.

        Returns the entry and whether it became missed just now: False
        when the local ring it matches had already been counted.
        """
        for entry in self.entries:
            if (entry.panel == panel and not entry.reported
                    and abs(entry.time - when) <= MISSED_MATCH_WINDOW
                    and entry.outcome in (OUTCOME_RINGING, OUTCOME_UNANSWERED,
                                          OUTCOME_MISSED)):
                entry.reported = True
                if entry is self.ringing:
                    self.ringing = None
                if entry.outcome == OUTCOME_MISSED:
                    return entry, False
                entry.outcome = OUTCOME_MISSED
                self.missed_count += 1
                return entry, True
        entry = self._add(CallEntry(
            id=self._new_id(), time=when, panel=panel, name=name,
            outcome=OUTCOME_MISSED, reported=True))
        self.missed_count += 1
        return entry, True

    def clear_missed(self) -> None:
        """Reset the missed-call count. The history stays."""
        self.missed_count = 0

    # ─── what the sensor shows, and what is stored ───────────────────

    def recent(self) -> list[dict[str, str]]:
        """The entries, newest first, as the sensor's `recent` attribute."""
        return [entry.as_attribute() for entry in self.entries]

    def to_dict(self) -> dict[str, Any]:
        """The JSON-safe form persisted between restarts."""
        return {
            "missed_count": self.missed_count,
            "next_id": self._next_id,
            "entries": [
                {"id": e.id, "time": e.time, "panel": e.panel, "name": e.name,
                 "outcome": e.outcome, "reported": e.reported}
                for e in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: Any) -> CallLog:
        """Rebuild a stored log, dropping whatever does not read back.

        The store is a convenience, not a source of truth: a damaged one
        loses entries, never the integration's setup. A ring that was
        still current when Home Assistant stopped cannot be current now,
        and nothing will ever end it, so it comes back unanswered.
        """
        if not isinstance(data, Mapping):
            return cls()
        entries: list[CallEntry] = []
        raw_entries = data.get("entries")
        for raw in raw_entries if isinstance(raw_entries, list) else ():
            entry = _entry_from(raw)
            if entry is not None:
                entries.append(entry)
        count = data.get("missed_count")
        next_id = data.get("next_id")
        return cls(
            entries,
            missed_count=count if _is_int(count) and count > 0 else 0,
            next_id=next_id if _is_int(next_id) and next_id > 0 else 1)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_from(raw: Any) -> CallEntry | None:
    if not isinstance(raw, Mapping):
        return None
    entry_id, when = raw.get("id"), raw.get("time")
    panel, name = raw.get("panel"), raw.get("name")
    outcome = raw.get("outcome")
    if (not _is_int(entry_id)
            or not isinstance(when, (int, float)) or isinstance(when, bool)
            or not isinstance(panel, str) or not isinstance(name, str)
            or outcome not in OUTCOMES):
        return None
    if outcome == OUTCOME_RINGING:
        outcome = OUTCOME_UNANSWERED
    return CallEntry(id=entry_id, time=float(when), panel=panel[:_MAX_TEXT],
                     name=name[:_MAX_TEXT], outcome=outcome,
                     reported=bool(raw.get("reported", False)))
