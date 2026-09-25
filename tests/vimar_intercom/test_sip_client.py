"""Tests for the SIP layer's module state and its request loop.

sip_client.py imports no Home Assistant module, so it loads through the
stub package tests/conftest.py installs. Its state lives in module
globals that outlive a config entry, which is precisely why these two
behaviours need pinning down.
"""

import asyncio

import pytest

from custom_components.vimar_intercom import runtime
from custom_components.vimar_intercom import sip_client as sip


def run(coro):
    """Run one coroutine to completion on a private event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def restore_module_state(monkeypatch):
    """Give every test its own copy of the mutable module state."""
    monkeypatch.setattr(sip, "pending_incoming", dict(sip.pending_incoming))
    monkeypatch.setattr(sip, "call_state", dict(sip.call_state))
    monkeypatch.setattr(sip, "pending_transactions", {})
    monkeypatch.setattr(sip, "in_call", False)
    monkeypatch.setattr(sip, "calling", False)
    monkeypatch.setattr(sip, "registered", False)
    monkeypatch.setattr(sip, "registration_expiry", None)
    monkeypatch.setattr(sip, "writer", None)
    monkeypatch.setattr(sip, "reader", None)
    monkeypatch.setattr(sip, "lock", None)
    monkeypatch.setattr(sip, "cseq_counter", 0)
    monkeypatch.setattr(sip, "local_tag", None)
    monkeypatch.setattr(sip, "_state_change_callback", None)
    monkeypatch.setattr(sip, "_broadcast", None)


# ─── reset_state ─────────────────────────────────────────────────────

def test_reset_state_clears_a_call_that_outlived_its_socket():
    """A reload mid-call used to leave `in_call` True forever."""
    sip.in_call = True
    sip.calling = True
    sip.registered = True
    sip.registration_expiry = 1e9
    sip.call_state["call_id"] = "call-abc"
    sip.pending_incoming["active"] = True
    sip.pending_transactions["k"] = asyncio.Queue()

    sip.reset_state()

    assert sip.in_call is False
    assert sip.calling is False
    assert sip.registered is False
    assert sip.registration_expiry is None
    assert sip.is_registered() is False
    assert sip.call_state["call_id"] is None
    assert sip.pending_incoming["active"] is False
    assert sip.pending_transactions == {}


def test_reset_state_drops_the_callbacks_of_the_previous_entry():
    """They point at a torn-down hub and its removed entities."""
    sip.set_state_callback(lambda: None)
    sip.init(lambda *_: None)

    sip.reset_state()

    assert sip._state_change_callback is None
    assert sip._broadcast is None


# ─── do_register never opens a socket of its own ─────────────────────

def test_do_register_refuses_to_connect_on_its_own(monkeypatch):
    """A socket opened here would have no reader and be orphaned."""
    connected = []

    async def _connect():
        connected.append(True)

    monkeypatch.setattr(sip, "connect", _connect)

    assert run(sip.do_register()) is False
    assert connected == []


class _ClosingWriter:
    def is_closing(self):
        return True


def test_do_register_refuses_on_a_closing_writer(monkeypatch):
    connected = []

    async def _connect():
        connected.append(True)

    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "writer", _ClosingWriter())

    assert run(sip.do_register()) is False
    assert connected == []


# ─── the request processor outlives a failed reply ───────────────────

def test_request_processor_survives_a_failing_request(monkeypatch):
    """A send failure while answering an INFO used to kill the loop.

    The supervisor then reconnects and re-registers happily while no
    incoming SIP request is ever processed again.
    """
    handled: list[str] = []

    async def _one():
        raw = await sip.incoming_requests.get()
        if raw == "boom":
            raise ConnectionResetError("writer is closing")
        handled.append(raw)

    monkeypatch.setattr(sip, "_process_one_request", _one)

    async def scenario():
        monkeypatch.setattr(sip, "incoming_requests", asyncio.Queue())
        task = asyncio.create_task(sip.request_processor())
        for item in ("first", "boom", "second"):
            await sip.incoming_requests.put(item)
        # Let the loop drain the queue.
        for _ in range(10):
            await asyncio.sleep(0)
            if not sip.incoming_requests.qsize():
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert handled == ["first", "second"]


# ─── a call always ends locally, whether or not the BYE goes out ─────

QR_FIELDS = {
    "ID": "60901",
    "PWD": "examplepassword",
    "CDOMAIN": "example.invalid",
}


@pytest.fixture
def live_call(monkeypatch):
    """A configured SIP layer holding one established call.

    Returns the list the media teardown and the broadcasts are recorded
    in, so a test can tell an ending that reached the entities from one
    that stopped halfway.
    """
    events: list[str] = []

    config = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {})
    monkeypatch.setattr(sip, "CFG", config)
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.5")

    async def _stop_media():
        events.append("stop_media")

    async def _broadcast(msg_type, _msg):
        events.append(msg_type)

    monkeypatch.setattr(sip.media, "stop_media", _stop_media)
    monkeypatch.setattr(sip, "_broadcast", _broadcast)

    sip.in_call = True
    sip.call_state.update(call_id="call-abc", from_tag="ftag", to_tag="ttag")
    return events


def test_a_bye_that_cannot_be_sent_still_ends_the_call(live_call, monkeypatch):
    """The wedge this guards: `in_call` True with no call behind it.

    The cloud connection drops mid-call, so `send` raises on a closed
    writer. The clear-up used to sit after that send and never ran, and
    the stuck `in_call` then stopped the camera calling again and made
    every real doorbell press a 603 until the entry was reloaded.
    """
    async def _raises(_msg):
        raise ConnectionResetError("the SIP socket has gone")

    monkeypatch.setattr(sip, "send", _raises)

    with pytest.raises(ConnectionResetError):
        run(sip.do_hangup())

    assert sip.in_call is False
    assert sip.calling is False
    assert sip.call_state["call_id"] is None
    assert live_call == ["stop_media", "call_ended"]
    assert sip.pending_transactions == {}


def test_a_bye_that_is_sent_still_ends_the_call(live_call, monkeypatch):
    """The happy path must not have changed."""
    sent: list[str] = []

    async def _send(msg):
        sent.append(msg.split(" ", 1)[0])

    monkeypatch.setattr(sip, "send", _send)

    run(sip.do_hangup())

    assert sent == ["BYE"]
    assert sip.in_call is False
    assert sip.call_state["call_id"] is None
    assert live_call == ["stop_media", "call_ended"]


def test_losing_the_connection_mid_call_ends_the_call(live_call):
    """The dialog lived on the socket that just went."""
    sip.pending_incoming["active"] = True

    run(sip._abandon_call())

    assert sip.in_call is False
    assert sip.calling is False
    assert sip.call_state["call_id"] is None
    assert sip.pending_incoming["active"] is False
    assert live_call == ["stop_media", "call_ended"]


def test_a_reconnect_with_no_call_up_broadcasts_nothing(live_call):
    """Otherwise every retry of an outage would fire `call_ended`."""
    sip.in_call = False
    sip.call_state.update(call_id=None, from_tag=None, to_tag=None)

    run(sip._abandon_call())

    assert live_call == []


def test_the_supervisor_ends_the_call_when_the_connection_drops(
        live_call, monkeypatch):
    """The supervisor's failure path used to clear registration only."""
    async def _connect():
        return None

    async def _do_register():
        return True

    async def _reader_loop():
        raise ConnectionResetError("the cloud dropped us")

    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "do_register", _do_register)
    monkeypatch.setattr(sip, "_reader_loop", _reader_loop)
    monkeypatch.setattr(sip, "reconnect_delay", lambda _attempt: 0)

    async def scenario():
        task = asyncio.create_task(sip.connection_supervisor())
        for _ in range(100):
            await asyncio.sleep(0)
            if not sip.in_call:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())

    assert sip.in_call is False
    assert sip.call_state["call_id"] is None
    assert "call_ended" in live_call
