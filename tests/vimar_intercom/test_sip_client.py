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


# ─── a hang-up must not strip a dialog that is still being set up ────

async def _no_wait(*_args, **_kwargs):
    """Stand in for `_wait_final`: the far end answers at once."""
    return []


def test_hanging_up_while_the_invite_is_in_flight_keeps_the_call_id(
        live_call, monkeypatch):
    """`do_call` owns `call_state` until its INVITE transaction is over.

    The camera opens the stream, the auto-call sends an INVITE, and the
    2xx can take up to 45 s. A hang-up inside that window used to run
    the local teardown, wiping the Call-ID the arriving 2xx completes
    the dialog around — `do_call` sets the To-tag and the remote
    Contact but never re-sets the Call-ID.
    """
    sent: list[str] = []

    async def _send(msg):
        sent.append(msg)

    monkeypatch.setattr(sip, "send", _send)

    sip.in_call = False
    sip.calling = True
    sip.call_state.update(call_id="call-abc", from_tag="ftag",
                          to_tag=None, remote_contact=None)

    run(sip.do_hangup())

    assert sip.calling is False
    assert sip.call_state["call_id"] == "call-abc"
    assert sip.call_state["from_tag"] == "ftag"
    # Nothing was torn down, and no BYE can be sent for a dialog that
    # has not reached its 2xx yet.
    assert sent == []
    assert live_call == []


def test_a_hang_up_during_setup_leaves_a_call_a_later_bye_can_end(
        live_call, monkeypatch):
    """The cost of the stranded dialog: the panel's single registration.

    Losing the Call-ID took the BYE with it — every later hang-up, the
    button, the five-minute limit and the unload alike, found no
    `call_id` and took the early-return branch.
    """
    sent: list[str] = []

    async def _send(msg):
        sent.append(msg)

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "_wait_final", _no_wait)

    sip.in_call = False
    sip.calling = True
    sip.call_state.update(call_id="call-abc", from_tag="ftag",
                          to_tag=None, remote_contact=None)

    run(sip.do_hangup())

    # The 2xx arrives: `do_call` fills in the far end and marks it up.
    sip.call_state.update(to_tag="ttag",
                          remote_contact="sip:panel@example.invalid")
    sip.in_call = True

    run(sip.do_hangup())

    assert [msg.split(" ", 1)[0] for msg in sent] == ["BYE"]
    assert "Call-ID: call-abc\r\n" in sent[0]
    assert ";tag=ttag" in sent[0]
    assert sip.in_call is False
    assert sip.call_state["call_id"] is None
    assert live_call == ["stop_media", "call_ended"]


# ─── an incoming BYE ends the dialog it names, and only that one ─────

def _raw_bye(call_id: str) -> str:
    """One well-formed BYE from the panel for `call_id`."""
    return (
        "BYE sip:60901@example.invalid SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bK-panel\r\n"
        "From: <sip:panel@example.invalid>;tag=ptag\r\n"
        "To: <sip:60901@example.invalid>;tag=ftag\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 2 BYE\r\n"
        "Content-Length: 0\r\n\r\n")


def test_a_bye_we_cannot_acknowledge_still_ends_the_call(
        live_call, monkeypatch):
    """The other half of "a call always ends locally".

    The panel hangs up as the cloud connection goes, so the 200 OK
    raises on a dead writer. The clear-up used to sit after that send:
    `in_call` stayed True with no dialog behind it, sticking the in-call
    sensor on, stopping the camera placing a call and turning every
    later doorbell press into a 603.
    """
    async def _raises(_msg):
        raise ConnectionResetError("the SIP socket has gone")

    monkeypatch.setattr(sip, "send", _raises)

    with pytest.raises(ConnectionResetError):
        run(sip.handle_incoming_bye(_raw_bye("call-abc")))

    assert sip.in_call is False
    assert sip.call_state["call_id"] is None
    assert live_call == ["stop_media", "call_ended"]


def test_a_bye_for_another_dialog_leaves_the_live_call_alone(
        live_call, monkeypatch):
    """A late or duplicate BYE used to tear down whatever was up.

    The handler never looked at the Call-ID, so a BYE belonging to the
    previous call ended the current one — and, when that current one was
    still being set up, left it with no identity at all.
    """
    sent: list[str] = []

    async def _send(msg):
        sent.append(msg)

    monkeypatch.setattr(sip, "send", _send)

    run(sip.handle_incoming_bye(_raw_bye("call-that-already-ended")))

    # The panel still gets its answer.
    assert len(sent) == 1
    assert sent[0].startswith("SIP/2.0 200 OK")
    assert "Call-ID: call-that-already-ended\r\n" in sent[0]
    # But the call that is actually up is untouched.
    assert sip.in_call is True
    assert sip.call_state["call_id"] == "call-abc"
    assert live_call == []
