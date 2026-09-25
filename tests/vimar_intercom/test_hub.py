"""Tests for the hub's call state machine.

hub.py imports only sip_client, media_handler, const and runtime — none
of which import Home Assistant (see tests/conftest.py) — so it loads
through the same stub-package mechanism as the other pure modules and
is unit-testable directly, without a running Home Assistant.

The SIP and media layers keep their state in module globals, so every
test that touches them installs its own stubs through the `sip_stub`
fixture and gets the real attributes back afterwards.
"""

import asyncio

import pytest

from custom_components.vimar_intercom import hub, media_handler, runtime
from custom_components.vimar_intercom import sip_client as sip

QR_FIELDS = {
    "ID": "60901",
    "PWD": "examplepassword",
    "CDOMAIN": "example.invalid",
}


def _hub(panels: str = "55001:Front Door,55002:Garage") -> hub.VimarIntercomHub:
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"panels": panels})
    return hub.VimarIntercomHub(cfg)


def run(coro):
    """Run one coroutine to completion on a private event loop."""
    return asyncio.run(coro)


class RecordingBus:
    """Stands in for `hass.bus`, collecting the events fired on it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type, data):
        self.events.append((event_type, data))


class FakeHass:
    """The only part of Home Assistant the hub touches."""

    def __init__(self) -> None:
        self.bus = RecordingBus()


@pytest.fixture
def sip_stub(monkeypatch):
    """Replace the SIP module's state and operations with recorded stubs."""
    calls: list[str] = []

    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "pending_incoming", dict(sip.pending_incoming))
    monkeypatch.setattr(sip, "call_state", dict(sip.call_state))
    monkeypatch.setattr(sip, "is_registered", lambda: True)

    async def _do_call(target=None):
        calls.append(f"call:{target}")
        return True, "Connected"

    async def _do_answer():
        calls.append("answer")
        return True, "Answered"

    async def _do_decline(busy=False):
        calls.append("decline:486" if busy else "decline:603")

    async def _do_hangup():
        calls.append("hangup")

    async def _do_register():
        calls.append("register")
        return True

    monkeypatch.setattr(sip, "do_call", _do_call)
    monkeypatch.setattr(sip, "do_answer_incoming", _do_answer)
    monkeypatch.setattr(sip, "do_decline_incoming", _do_decline)
    monkeypatch.setattr(sip, "do_hangup", _do_hangup)
    monkeypatch.setattr(sip, "do_register", _do_register)
    monkeypatch.setattr(sip, "request_reconnect",
                        lambda: calls.append("reconnect"))
    return calls


# ─── _panel_for ──────────────────────────────────────────────────────

def test_panel_for_matches_a_configured_panel():
    h = _hub()
    assert h._panel_for("sip:55001@example.invalid") == (
        "55001", "Front Door")


def test_panel_for_echoes_the_address_when_the_caller_is_unconfigured():
    h = _hub()
    assert h._panel_for("sip:99999@example.invalid") == ("99999", "99999")


def test_panel_for_of_an_empty_uri_is_unknown():
    h = _hub()
    assert h._panel_for("") == ("", "unknown")


def test_panel_for_of_a_malformed_uri_with_no_at_sign():
    h = _hub()
    # No "@" at all: the whole string (after stripping any "sip:"
    # prefix) is treated as the address.
    assert h._panel_for("not-a-uri") == ("not-a-uri", "not-a-uri")


# ─── the auto-call flag ──────────────────────────────────────────────

def test_the_panel_ending_the_call_clears_the_auto_call(sip_stub):
    h = _hub()
    h._auto_called = True
    h._auto_call_target = "55001"
    run(h._handle_broadcast("call_ended", "Call ended"))
    assert h._auto_called is False
    assert h._auto_call_target is None


def test_a_stream_that_never_got_a_call_clears_the_auto_call(sip_stub):
    """The 503 path: the view gives up and closes with no call up."""
    h = _hub()
    h._stream_viewers = 1
    h._auto_called = True
    run(h.stream_closed())
    assert h._auto_called is False


def test_a_real_ring_survives_a_stream_that_timed_out(sip_stub):
    """Regression: a stale auto-call flag declined every later visitor."""
    h = _hub()
    hass = FakeHass()
    h.set_hass(hass, "entry123")

    async def scenario():
        # A viewer opens the camera and the call never establishes.
        await h.stream_opened()
        await asyncio.sleep(0)  # let the auto-call task run
        sip.in_call = False
        sip.calling = False
        await h.stream_closed()
        # Now somebody actually presses the doorbell.
        sip.pending_incoming.update(
            active=True, caller_uri="sip:55001@example.invalid")
        await h._handle_broadcast("ring", "Incoming call")

    run(scenario())

    assert not any(c.startswith("decline") for c in sip_stub)
    assert hass.bus.events == [
        ("vimar_intercom_ring",
         {"panel": "55001", "panel_name": "Front Door",
          "entry_id": "entry123"}),
    ]


def test_a_second_viewer_does_not_place_a_second_call(sip_stub):
    """Both viewers arrive before `calling` is set by the first do_call."""
    h = _hub()

    async def scenario():
        await h.stream_opened()
        await h.stream_opened()
        await asyncio.sleep(0)

    run(scenario())
    assert sip_stub == ["call:None"]
    assert h._stream_viewers == 2


def test_an_established_call_is_hung_up_when_nobody_waited(
        sip_stub, monkeypatch):
    """The view waits 15 s; do_call may take 45. Nobody is left to watch.

    The grace timer has to run to an actual BYE: a call that established
    with nobody watching otherwise holds the account's single
    registration until the five-minute duration limit expires.
    """
    h = _hub()
    monkeypatch.setattr(hub, "STREAM_HANGUP_DELAY", 0)
    h._auto_called = True
    h._stream_viewers = 0

    async def scenario():
        await h._do_auto_call(None)
        assert h._hangup_task is not None
        sip.in_call = True
        await h._hangup_task

    run(scenario())
    assert sip_stub == ["call:None", "hangup"]
    assert h._auto_called is False


def test_a_bye_that_cannot_be_sent_still_clears_the_auto_call(
        sip_stub, monkeypatch):
    """The grace period expires while the SIP socket is gone.

    `sip.send` raises on a closed writer, which is the normal state
    during the outage this timer is most likely to fire in. If that
    escaped, `_auto_called` would survive and every later doorbell press
    would be declined as the echo of a call that ended long ago.
    """
    h = _hub()
    h._auto_called = True
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setattr(hub, "STREAM_HANGUP_DELAY", 0)

    async def _raises():
        raise ConnectionResetError("the SIP socket has gone")

    monkeypatch.setattr(sip, "do_hangup", _raises)

    run(h._delayed_hangup())
    assert h._auto_called is False


def test_the_duration_limit_clears_the_auto_call_even_if_the_bye_fails(
        sip_stub, monkeypatch):
    h = _hub()
    h._auto_called = True
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setattr(hub, "MAX_CALL_DURATION", 0)

    async def _raises():
        raise ConnectionResetError("the SIP socket has gone")

    monkeypatch.setattr(sip, "do_hangup", _raises)

    run(h._call_timeout())
    assert h._auto_called is False


def test_a_viewer_returning_inside_the_grace_period_keeps_the_call(sip_stub):
    """A cancelled grace period must not forget that the call is ours."""
    h = _hub()

    async def scenario():
        h._auto_called = True
        h._schedule_delayed_hangup()
        await asyncio.sleep(0)
        h._hangup_task.cancel()
        await asyncio.gather(h._hangup_task, return_exceptions=True)

    run(scenario())
    assert h._auto_called is True
    assert "hangup" not in sip_stub


# ─── SIP glare: answer what is already ringing ───────────────────────

def test_opening_the_stream_during_a_ring_answers_it(sip_stub):
    h = _hub()
    sip.pending_incoming["active"] = True

    async def scenario():
        await h.stream_opened()
        await asyncio.sleep(0)

    run(scenario())
    assert sip_stub == ["answer"]


def test_opening_the_stream_with_nothing_ringing_calls_the_panel(sip_stub):
    h = _hub()

    async def scenario():
        await h.stream_opened("55002")
        await asyncio.sleep(0)

    run(scenario())
    assert sip_stub == ["call:sip:55002@example.invalid"]


# ─── the door ────────────────────────────────────────────────────────

def test_the_door_asks_for_a_reconnect_instead_of_re_registering(
        sip_stub, monkeypatch):
    """Re-registering here would open a TLS socket nobody ever reads."""
    h = _hub()
    monkeypatch.setattr(sip, "is_registered", lambda: False)

    async def _fails(*args, **kwargs):
        return False, "Not registered"

    monkeypatch.setattr(sip, "do_system_message", _fails)

    ok, msg = run(h.async_door())
    assert ok is False
    assert "register" not in sip_stub
    assert "reconnect" in sip_stub
    assert "reconnect" in msg.lower()


def _record_door(monkeypatch, results=None):
    """Record the exact (uri, body) of every door command sent.

    The old door tests captured the URI and then asserted only how many
    attempts there were, or stubbed `do_system_message` away entirely.
    Either way the three branches of `async_door` could be swapped for
    each other and the suite stayed green — for the one function in this
    integration that opens a door.
    """
    attempts: list[tuple[str, str]] = []
    outcomes = list(results or [(True, "OK (200)")])

    async def _send(uri, body, extra_headers=None):
        attempts.append((uri, body))
        return outcomes.pop(0) if outcomes else (True, "OK (200)")

    monkeypatch.setattr(sip, "do_system_message", _send)
    return attempts


DOOR_GROUP_URI = "sip:21@example.invalid"


def test_the_door_of_an_explicit_panel_gets_open_current(
        sip_stub, monkeypatch):
    """The per-panel button, for a plant with more than one entrance."""
    attempts = _record_door(monkeypatch)

    ok, _ = run(_hub().async_door(target="55002"))

    assert ok is True
    assert attempts == [("sip:55002@example.invalid", "OPEN_CURRENT")]


def test_the_door_during_a_call_gets_open_current_on_the_relay_group(
        sip_stub, monkeypatch):
    """OPEN_CURRENT opens the relay of whichever panel is calling."""
    attempts = _record_door(monkeypatch)
    monkeypatch.setattr(sip, "in_call", True)

    ok, _ = run(_hub().async_door())

    assert ok is True
    assert attempts == [(DOOR_GROUP_URI, "OPEN_CURRENT")]


def test_the_door_outside_a_call_gets_the_configured_command(
        sip_stub, monkeypatch):
    """No call, no target: the main entrance, with the configured command."""
    attempts = _record_door(monkeypatch)
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS),
        {"panels": "55001:Front Door", "door_command": "OPEN_1F"})

    ok, _ = run(hub.VimarIntercomHub(cfg).async_door())

    assert ok is True
    assert attempts == [(DOOR_GROUP_URI, "OPEN_1F")]


def test_the_door_retries_once_on_an_explicit_rejection(
        sip_stub, monkeypatch):
    """A response that says the command was refused is safe to resend."""
    attempts = _record_door(
        monkeypatch, [(False, "Rejected with 500"), (True, "OK (200)")])

    ok, _ = run(_hub().async_door())

    assert ok is True
    assert attempts == [(DOOR_GROUP_URI, "OPEN_2F"),
                        (DOOR_GROUP_URI, "OPEN_2F")]
    assert "register" not in sip_stub


def test_a_door_command_that_got_no_reply_is_never_resent(
        sip_stub, monkeypatch):
    """A MESSAGE whose 200 OK was lost looks exactly like one that never
    arrived. Resending it pulses the relay a second time: the door
    opens, closes and opens again, the second time unattended."""
    attempts = _record_door(monkeypatch, [(False, sip.NO_RESPONSE)])

    ok, msg = run(_hub().async_door())

    assert ok is False
    assert len(attempts) == 1
    assert "may have opened" in msg


def test_the_retry_re_resolves_the_target_when_the_call_ended(
        sip_stub, monkeypatch):
    """The first attempt can take 30 s, and the call may not survive it.

    Choosing the branch once and reusing it sent OPEN_CURRENT to the
    relay group with no current call at all.
    """
    attempts: list[tuple[str, str]] = []
    outcomes = [(False, "Rejected with 500"), (True, "OK (200)")]

    async def _send(uri, body, extra_headers=None):
        attempts.append((uri, body))
        # The call ends while the first attempt is in flight.
        sip.in_call = False
        return outcomes.pop(0)

    monkeypatch.setattr(sip, "do_system_message", _send)
    monkeypatch.setattr(sip, "in_call", True)

    ok, _ = run(_hub().async_door())

    assert ok is True
    assert attempts == [(DOOR_GROUP_URI, "OPEN_CURRENT"),
                        (DOOR_GROUP_URI, "OPEN_2F")]


# ─── a real ring during a call is declined, but still announced ──────

def test_a_real_ring_during_a_call_still_fires_the_event(sip_stub, monkeypatch):
    """One call at a time is real; a silent visitor is not acceptable.

    `vimar_intercom_ring` is the only signal a notification bridge has,
    so a visitor arriving during a call — or during the up-to-45 s
    window where `calling` is set — used to be completely invisible.
    """
    h = _hub()
    hass = FakeHass()
    h.set_hass(hass, "entry123")
    monkeypatch.setattr(sip, "in_call", True)
    # The call up is one we answered, not one we placed.
    sip.pending_incoming.update(
        active=True, caller_uri="sip:55002@example.invalid")

    run(h._handle_broadcast("ring", "Incoming call"))

    # Declined, because there is only one line — but with 486 Busy Here,
    # which leaves the rest of the plant ringing.
    assert sip_stub == ["decline:486"]
    assert hass.bus.events == [
        ("vimar_intercom_ring",
         {"panel": "55002", "panel_name": "Garage",
          "entry_id": "entry123"}),
    ]


def test_the_ring_callbacks_also_run_for_a_ring_during_a_call(
        sip_stub, monkeypatch):
    h = _hub()
    seen: list[str] = []
    h.register_ring_callback(seen.append)
    monkeypatch.setattr(sip, "in_call", True)
    sip.pending_incoming.update(
        active=True, caller_uri="sip:55001@example.invalid")

    run(h._handle_broadcast("ring", "Incoming call"))

    assert seen == ["55001"]


def test_the_pbx_echoing_our_own_call_fires_nothing(sip_stub, monkeypatch):
    """After we INVITE a panel the PBX INVITEs us back. Not a doorbell."""
    h = _hub()
    hass = FakeHass()
    h.set_hass(hass, "entry123")
    h._auto_called = True
    h._auto_call_target = "55002"
    monkeypatch.setattr(sip, "calling", True)
    sip.pending_incoming.update(
        active=True, caller_uri="sip:55002@example.invalid")

    run(h._handle_broadcast("ring", "Incoming call"))

    # Silent, and declined with 603: we already have that call.
    assert sip_stub == ["decline:603"]
    assert hass.bus.events == []


def test_the_echo_of_a_call_placed_with_no_explicit_target_is_silent(
        sip_stub, monkeypatch):
    """`do_call()` records the default panel in `call_state` only."""
    h = _hub()
    hass = FakeHass()
    h.set_hass(hass, "entry123")
    h._auto_called = True
    monkeypatch.setattr(sip, "calling", True)
    sip.call_state["original_target"] = "sip:55001@example.invalid"
    sip.pending_incoming.update(
        active=True, caller_uri="sip:55001@example.invalid")

    run(h._handle_broadcast("ring", "Incoming call"))

    assert sip_stub == ["decline:603"]
    assert hass.bus.events == []


# ─── teardown ────────────────────────────────────────────────────────

def test_stopping_hangs_up_a_live_call(sip_stub, monkeypatch):
    """Otherwise the panel holds a call whose socket has just gone."""
    h = _hub()
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setattr(sip, "writer", None)

    async def _noop():
        pass

    monkeypatch.setattr(media_handler, "stop_media", _noop)
    monkeypatch.setattr(media_handler, "close_transports", lambda: None)

    run(h.async_stop())
    assert "hangup" in sip_stub
    assert h._auto_called is False
    assert h._stream_viewers == 0


def test_a_hang_up_that_never_returns_cannot_block_the_unload(
        sip_stub, monkeypatch):
    """A half-open socket used to leave the entry stuck in "unloading".

    `sip.send` waits on the connection lock and then on `writer.drain()`,
    neither of them bounded, and the reader loop's keepalive holds that
    lock while blocked in a drain of its own. The reader is still alive,
    because this hang-up deliberately runs before the cancel loop.
    """
    h = _hub()
    monkeypatch.setattr(sip, "in_call", True)
    monkeypatch.setattr(sip, "writer", None)
    monkeypatch.setattr(hub, "HANGUP_ON_UNLOAD_TIMEOUT", 0.01)

    async def _never_returns():
        sip_stub.append("hangup")
        await asyncio.Event().wait()

    monkeypatch.setattr(sip, "do_hangup", _never_returns)

    async def _noop():
        pass

    monkeypatch.setattr(media_handler, "stop_media", _noop)
    monkeypatch.setattr(media_handler, "close_transports", lambda: None)

    run(asyncio.wait_for(h.async_stop(), 5))
    assert "hangup" in sip_stub
    assert h._stream_viewers == 0
