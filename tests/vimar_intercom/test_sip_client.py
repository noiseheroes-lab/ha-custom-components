"""Tests for the SIP layer's module state and its request loop.

sip_client.py imports no Home Assistant module, so it loads through the
stub package tests/conftest.py installs. Its state lives in module
globals that outlive a config entry, which is precisely why these two
behaviours need pinning down.
"""

import asyncio
import logging
import ssl
import time
from types import SimpleNamespace

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


# ─── a hang-up during setup is deferred, not dropped ─────────────────

def _raw_200_with_sdp() -> str:
    """The panel answering an INVITE, with an SDP answer attached."""
    body = ("v=0\r\n"
            "o=- 1 1 IN IP4 192.0.2.9\r\n"
            "c=IN IP4 192.0.2.9\r\n"
            "m=audio 40000 RTP/SAVP 0\r\n"
            "m=video 40002 RTP/SAVP 96\r\n")
    return (
        "SIP/2.0 200 OK\r\n"
        "Via: SIP/2.0/TLS 192.0.2.5:5070;branch=z9hG4bK-ours\r\n"
        "From: <sip:60901@example.invalid>;tag=ftag\r\n"
        "To: <sip:55001@example.invalid>;tag=paneltag\r\n"
        "Call-ID: call-abc\r\n"
        "CSeq: 1 INVITE\r\n"
        "Contact: <sip:55001@192.0.2.9:5060;transport=tls>\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(body.encode())}\r\n\r\n{body}")


@pytest.fixture
def configured(monkeypatch):
    """A registered SIP layer with a configuration and no real socket."""
    config = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {})
    monkeypatch.setattr(sip, "CFG", config)
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.5")
    monkeypatch.setattr(sip, "registered", True)
    monkeypatch.setattr(sip, "registration_expiry", time.monotonic() + 3600)
    return config


async def _first_transaction_queue() -> asyncio.Queue:
    """Wait for the transaction the operation under test just opened."""
    for _ in range(50):
        await asyncio.sleep(0)
        if sip.pending_transactions:
            return next(iter(sip.pending_transactions.values()))
    raise AssertionError("no transaction was opened")


def test_a_hang_up_during_setup_is_honoured_when_the_call_connects(
        configured, monkeypatch):
    """The press must survive the up-to-45 s window, not be discarded.

    `do_hangup` cannot tear down a dialog `do_call` still owns, so it
    records the request instead. Dropping it let the call establish and
    run to the five-minute limit, holding the account's single SIP
    registration while the hub declined every real doorbell press.
    """
    sent: list[str] = []
    media_setups: list[object] = []
    events: list[str] = []

    async def _send(msg):
        sent.append(msg)

    async def _setup_media(remote):
        media_setups.append(remote)

    async def _stop_media():
        events.append("stop_media")

    async def _broadcast(msg_type, _msg):
        events.append(msg_type)

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "_wait_final", _no_wait)
    monkeypatch.setattr(sip.media, "setup_media", _setup_media)
    monkeypatch.setattr(sip.media, "stop_media", _stop_media)
    monkeypatch.setattr(sip, "_broadcast", _broadcast)

    async def scenario():
        task = asyncio.create_task(sip.do_call())
        queue = await _first_transaction_queue()
        # The INVITE is in flight; the user gives up and presses Hang up.
        await sip.do_hangup()
        assert sip.hangup_requested is True
        # Only now does the panel answer.
        await queue.put(_raw_200_with_sdp())
        return await task

    ok, msg = run(scenario())

    assert ok is False
    assert "hung up" in msg.lower()
    # The dialog was completed far enough to end it properly: the ACK
    # went, then the BYE.
    methods = [m.split(" ", 1)[0] for m in sent]
    assert methods == ["INVITE", "ACK", "BYE"]
    assert "Call-ID: call-" in sent[-1]
    # No media was ever started for a call nobody is watching.
    assert media_setups == []
    assert sip.in_call is False
    assert sip.calling is False
    assert sip.hangup_requested is False
    assert "call_started" not in events
    assert events[-1] == "call_ended"


def test_a_hang_up_recorded_by_a_failed_call_does_not_end_the_next_one(
        configured, monkeypatch):
    """A rejected INVITE never reaches `_end_call_locally` to clear it."""
    sip.calling = True
    sip.call_state["call_id"] = "call-that-was-rejected"

    async def _send(_msg):
        pass

    monkeypatch.setattr(sip, "send", _send)
    run(sip.do_hangup())
    assert sip.hangup_requested is True

    async def scenario():
        task = asyncio.create_task(sip.do_call())
        await _first_transaction_queue()
        # `do_call` clears the stale request before it can act on it.
        assert sip.hangup_requested is False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())


# ─── Content-Length is a byte count ──────────────────────────────────

def test_content_length_counts_bytes_not_characters(configured, monkeypatch):
    """One accented character used to corrupt the whole SIP stream.

    `send` encodes the message as UTF-8, so a character count
    under-declares the body and the proxy frames the surplus bytes as
    the head of the next request. `door_command` is user-supplied.
    """
    sent: list[str] = []

    async def _send(msg):
        sent.append(msg)

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "_wait_final", _no_wait)

    body = "APRÌ_2F"
    run(sip.do_system_message("sip:21@example.invalid", body))

    head, _, wire_body = sent[0].partition("\r\n\r\n")
    declared = int([line.split(":", 1)[1] for line in head.split("\r\n")
                    if line.lower().startswith("content-length:")][0])
    assert declared == len(body.encode())
    assert declared != len(body)
    assert declared == len(wire_body.encode())


# ─── a challenge is answered with the header that matches its code ───

def _raw_challenge(code: int, header: str) -> str:
    return (
        f"SIP/2.0 {code} Unauthorized\r\n"
        "Via: SIP/2.0/TLS 192.0.2.5:5070;branch=z9hG4bK-ours\r\n"
        "From: <sip:60901@example.invalid>;tag=ftag\r\n"
        "To: <sip:21@example.invalid>;tag=servertag\r\n"
        "Call-ID: sys-abc\r\n"
        "CSeq: 1 MESSAGE\r\n"
        f'{header}: Digest realm="example.invalid", '
        'nonce="abc123", qop="auth"\r\n'
        "Content-Length: 0\r\n\r\n")


def _run_challenged_message(monkeypatch, code, header):
    """Send one MESSAGE, answer it with `code`, and return what went out."""
    sent: list[str] = []
    responses = [[_raw_challenge(code, header)], []]

    async def _send(msg):
        sent.append(msg)

    async def _wait(*_args, **_kwargs):
        return responses.pop(0) if responses else []

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "_wait_final", _wait)
    run(sip.do_system_message("sip:21@example.invalid", "OPEN_2F"))
    return sent


def test_a_401_challenge_is_answered_with_authorization(
        configured, monkeypatch):
    """A 401 answered with Proxy-Authorization is simply ignored.

    The retry is rejected identically, `do_system_message` reports
    "Rejected with 401", and the door silently does not open.
    """
    sent = _run_challenged_message(monkeypatch, 401, "WWW-Authenticate")
    assert len(sent) == 2
    assert "\r\nAuthorization: Digest " in sent[1]
    assert "\r\nProxy-Authorization:" not in sent[1]


def test_a_407_challenge_is_answered_with_proxy_authorization(
        configured, monkeypatch):
    """The proxy's own challenge keeps the header it expects."""
    sent = _run_challenged_message(monkeypatch, 407, "Proxy-Authenticate")
    assert len(sent) == 2
    assert "\r\nProxy-Authorization: Digest " in sent[1]


# ─── declining: 486 for a real visitor, 603 for our own echo ─────────

@pytest.fixture
def ringing(configured, monkeypatch):
    """One pending incoming INVITE, and the list of what we send back."""
    sent: list[str] = []

    async def _send(msg):
        sent.append(msg)

    monkeypatch.setattr(sip, "send", _send)
    sip.pending_incoming.update(
        active=True, cid="call-in", from_hdr="<sip:55001@example.invalid>",
        to_hdr="<sip:60901@example.invalid>", cseq="1 INVITE",
        via_block="Via: SIP/2.0/TLS 192.0.2.9:5060;branch=z9hG4bK-panel\r\n",
        my_tag="mytag", caller_uri="sip:55001@example.invalid")
    return sent


def test_declining_our_own_echo_uses_603(ringing):
    run(sip.do_decline_incoming())
    assert ringing[0].startswith("SIP/2.0 603 Decline")
    assert sip.pending_incoming["active"] is False


def test_declining_a_real_visitor_uses_486_busy_here(ringing):
    """603 is a global failure: a forking proxy cancels the other
    branches, so the indoor unit stops ringing too."""
    run(sip.do_decline_incoming(busy=True))
    assert ringing[0].startswith("SIP/2.0 486 Busy Here")
    assert sip.pending_incoming["active"] is False


# ─── framing is not left to the peer ─────────────────────────────────

def test_a_negative_content_length_cannot_re_slice_the_header(monkeypatch):
    """It made `total` smaller than the header and fed it back in."""
    reconnects: list[bool] = []
    monkeypatch.setattr(sip, "request_reconnect",
                        lambda: reconnects.append(True))
    raw = (b"SIP/2.0 200 OK\r\nCall-ID: x\r\nContent-Length: -400\r\n\r\n")
    assert run(sip._dispatch_buffer(raw)) == b""
    assert reconnects == [True]


def test_an_enormous_content_length_is_refused(monkeypatch):
    """Without a ceiling the reader buffers until the host runs out."""
    reconnects: list[bool] = []
    monkeypatch.setattr(sip, "request_reconnect",
                        lambda: reconnects.append(True))
    raw = (b"SIP/2.0 200 OK\r\nCall-ID: x\r\n"
           b"Content-Length: 99999999\r\n\r\n")
    assert run(sip._dispatch_buffer(raw)) == b""
    assert reconnects == [True]


# ─── TLS verification never fails open ───────────────────────────────

def test_a_missing_ca_file_is_refused_rather_than_unverified(
        monkeypatch, tmp_path):
    """It used to disable certificate validation on the door's socket."""
    monkeypatch.setattr(sip.C, "CA_PATH", str(tmp_path / "absent.pem"))
    with pytest.raises(FileNotFoundError):
        sip._create_ssl_context()


def test_the_shipped_ca_file_produces_a_verifying_context():
    ctx = sip._create_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# ─── Locating the SIP server (RFC 3263) ──────────────────────────────

VIMAR_SRV = [
    sip.locate.SrvRecord(0, 30, 7042, f"flexiprod{n}.ipvdes2.vimarsso.cloud")
    for n in (1, 2, 3)
]
VIMAR_SERVERS = {r.target for r in VIMAR_SRV}


class _RecordingWriter:
    def __init__(self, sockname=None):
        self.sent = b""
        self._sockname = sockname

    def get_extra_info(self, name, default=None):
        # A real StreamWriter always has this; connect() reads the local
        # address the connection leaves from through it.
        return self._sockname if name == "sockname" else default

    def write(self, data):
        self.sent += data

    async def drain(self):
        return None

    def is_closing(self):
        return False

    def close(self):
        return None


@pytest.fixture
def dialled(monkeypatch):
    """Record every TLS connection attempt instead of making it.

    Hosts listed in `dialled.dead` never answer, like the blackholed
    port that stalled the supervisor.
    """
    state = SimpleNamespace(attempts=[], dead=set(), sockname=None)

    async def _open_connection(host, port, **kwargs):
        state.attempts.append({"host": host, "port": port, **kwargs})
        if host in state.dead:
            await asyncio.Event().wait()
        return object(), _RecordingWriter(sockname=state.sockname)

    monkeypatch.setattr(asyncio, "open_connection", _open_connection)
    monkeypatch.setattr(sip, "MY_IP", "192.0.2.5")
    return state


def _srv_answers(monkeypatch, records):
    queries = []

    async def _query(name):
        queries.append(name)
        return list(records)

    monkeypatch.setattr(sip.locate, "_aiodns_srv", _query)
    return queries


def _cloud_config(options=None):
    fields = {**QR_FIELDS, "CPROXY": "ipvdes.vimar.cloud", "PROXY": "192.0.2.10"}
    return runtime.build_runtime_config(
        runtime.entry_data_from_qr(fields), options or {})


def test_the_cloud_proxy_is_dialled_through_srv_but_verified_as_the_domain(
        monkeypatch, dialled):
    """The owner's failure: v2 dialled the SIP domain itself, which is dead."""
    queries = _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())

    run(sip.connect())

    assert queries == ["_sips._tcp.ipvdes.vimar.cloud"]
    [attempt] = dialled.attempts
    assert attempt["host"] in VIMAR_SERVERS
    assert attempt["port"] == 7042
    # SNI and the certificate hostname check stay on the QR's name.
    assert attempt["server_hostname"] == "ipvdes.vimar.cloud"
    assert attempt["ssl"].check_hostname is True
    assert attempt["ssl"].verify_mode == ssl.CERT_REQUIRED


def test_the_sip_headers_still_name_the_domain_after_srv(monkeypatch, dialled):
    _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())

    async def _no_answer(*_args, **_kwargs):
        return []

    monkeypatch.setattr(sip, "_wait_final", _no_answer)

    async def _scenario():
        await sip.connect()
        await sip.do_register()

    run(_scenario())

    sent = sip.writer.sent.decode()
    assert sent.startswith("REGISTER sip:example.invalid SIP/2.0\r\n")
    assert "Route: <sip:ipvdes.vimar.cloud;transport=tls;lr>\r\n" in sent
    assert "vimarsso" not in sent


def test_a_dead_srv_server_is_skipped_for_the_next(monkeypatch, dialled):
    monkeypatch.setattr(sip.C, "SIP_CONNECT_TIMEOUT", 0.05)
    _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())
    # Whichever server the weighted draw picks first, make it the dead one.
    monkeypatch.setattr(
        sip.locate, "order_srv",
        lambda records, rng=None: [
            sip.locate.Target(r.target, r.port) for r in records])
    dialled.dead.add("flexiprod1.ipvdes2.vimarsso.cloud")

    run(sip.connect())

    assert [a["host"] for a in dialled.attempts] == [
        "flexiprod1.ipvdes2.vimarsso.cloud",
        "flexiprod2.ipvdes2.vimarsso.cloud",
    ]
    assert {a["server_hostname"] for a in dialled.attempts} == {
        "ipvdes.vimar.cloud"}


def test_a_connect_that_never_completes_raises_instead_of_hanging(
        monkeypatch, dialled):
    monkeypatch.setattr(sip.C, "SIP_CONNECT_TIMEOUT", 0.05)
    _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())
    dialled.dead.update(VIMAR_SERVERS)

    started = time.monotonic()
    with pytest.raises(ConnectionError):
        run(sip.connect())
    assert time.monotonic() - started < 2
    assert len(dialled.attempts) == 3


def test_every_reconnect_looks_the_servers_up_again(monkeypatch, dialled):
    queries = _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())

    run(sip.connect())
    run(sip.connect())

    assert len(queries) == 2


def test_without_srv_records_the_configured_host_is_dialled(monkeypatch, dialled):
    _srv_answers(monkeypatch, [])
    monkeypatch.setattr(sip, "CFG", _cloud_config({"sip_port": 5061}))

    run(sip.connect())

    [attempt] = dialled.attempts
    assert (attempt["host"], attempt["port"]) == ("ipvdes.vimar.cloud", 5061)


def test_a_port_override_applies_to_the_srv_servers(monkeypatch, dialled):
    _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config({"sip_port": 5061}))

    run(sip.connect())

    [attempt] = dialled.attempts
    assert attempt["host"] in VIMAR_SERVERS
    assert attempt["port"] == 5061


def test_the_local_panel_is_dialled_directly_without_srv(monkeypatch, dialled):
    async def _query(_name):
        raise AssertionError("the local panel must not go through SRV")

    monkeypatch.setattr(sip.locate, "_aiodns_srv", _query)
    monkeypatch.setattr(sip, "CFG", _cloud_config({"prefer_local": True}))

    run(sip.connect())

    [attempt] = dialled.attempts
    assert (attempt["host"], attempt["port"]) == ("192.0.2.10", 5060)
    assert attempt["server_hostname"] == "ipvdes.vimar.cloud"


def test_connecting_never_logs_a_credential(monkeypatch, dialled, caplog):
    _srv_answers(monkeypatch, VIMAR_SRV)
    config = _cloud_config()
    monkeypatch.setattr(sip, "CFG", config)

    with caplog.at_level(logging.DEBUG):
        run(sip.connect())

    assert "examplepassword" not in caplog.text
    assert config.sip_ha1 not in caplog.text
    assert "established with flexiprod" in caplog.text


# ─── the reader must be listening before the first REGISTER ──────────

def test_the_reader_is_running_before_the_first_register(monkeypatch):
    """The live failure no simulated test caught.

    The supervisor used to call do_register before starting the reader,
    and the reader is the only thing that hands a response to the
    transaction waiting for it. The registrar's 401 sat unread in the
    socket, every REGISTER timed out as "refused", and no installation
    could ever register.
    """
    reader_started = asyncio.Event()
    seen = {}

    async def _connect():
        return None

    async def _reader_loop():
        reader_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["reader_cancelled"] = True
            raise

    async def _do_register():
        try:
            await asyncio.wait_for(reader_started.wait(), 1)
        except TimeoutError:
            seen["reader_was_running"] = False
            return False
        seen["reader_was_running"] = True
        return True

    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "_reader_loop", _reader_loop)
    monkeypatch.setattr(sip, "do_register", _do_register)
    monkeypatch.setattr(sip, "reconnect_delay", lambda _attempt: 0)

    async def scenario():
        task = asyncio.create_task(sip.connection_supervisor())
        for _ in range(200):
            await asyncio.sleep(0)
            if "reader_was_running" in seen:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())

    assert seen.get("reader_was_running") is True
    # Shutting the supervisor down takes the connection's reader with it.
    assert seen.get("reader_cancelled") is True


def test_a_refused_registration_stops_the_reader_before_backing_off(
        monkeypatch):
    """A connection given up on must not keep dispatching messages."""
    readers = []
    registers = []

    async def _connect():
        return None

    async def _reader_loop():
        state = {"cancelled": False}
        readers.append(state)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def _do_register():
        registers.append([dict(r) for r in readers])
        await asyncio.sleep(0)
        return False

    monkeypatch.setattr(sip, "connect", _connect)
    monkeypatch.setattr(sip, "_reader_loop", _reader_loop)
    monkeypatch.setattr(sip, "do_register", _do_register)
    monkeypatch.setattr(sip, "reconnect_delay", lambda _attempt: 0)

    async def scenario():
        task = asyncio.create_task(sip.connection_supervisor())
        for _ in range(500):
            await asyncio.sleep(0)
            if len(registers) >= 2:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())

    assert len(registers) >= 2
    # When the second attempt registers, the first attempt's reader is
    # already stopped.
    assert registers[1][0]["cancelled"] is True


def test_the_local_address_comes_from_the_connection(monkeypatch, dialled):
    """Guessed once through DNS at startup, it could stay 0.0.0.0 forever."""
    _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())
    monkeypatch.setattr(sip, "MY_IP", "0.0.0.0")
    dialled.sockname = ("192.0.2.77", 51234)

    run(sip.connect())

    assert sip.MY_IP == "192.0.2.77"


def test_an_ipv6_source_keeps_the_previous_local_address(monkeypatch, dialled):
    """The SDP says IN IP4; an IPv6 source address would make it lie."""
    _srv_answers(monkeypatch, VIMAR_SRV)
    monkeypatch.setattr(sip, "CFG", _cloud_config())
    dialled.sockname = ("2001:db8::7", 51234, 0, 0)

    run(sip.connect())

    assert sip.MY_IP == "192.0.2.5"


# ─── an incoming MESSAGE is acknowledged, handed on, never logged ────

STATUS_BODY = ('GET_INIT_STATUS_REPLY;[{"PARAM":"token","VALUE":"secret-token"},'
               '{"PARAM":"rubrica_ver","VALUE":"abc"}]')


def _raw_message(body: str, panda: str | None = "blue") -> str:
    """One MESSAGE from the indoor unit carrying `body`."""
    panda_line = f"Panda: {panda}\r\n" if panda else ""
    encoded = body.encode()
    return (
        "MESSAGE sip:60901@example.invalid SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 198.51.100.7:5061;branch=z9hG4bK-picg\r\n"
        "From: <sip:60001@example.invalid>;tag=ptag\r\n"
        "To: <sip:60901@example.invalid>\r\n"
        "Call-ID: msg-1\r\n"
        "CSeq: 7 MESSAGE\r\n"
        f"{panda_line}"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(encoded)}\r\n\r\n{body}")


def _deliver(monkeypatch, raw: str):
    sent: list[str] = []
    seen: list[tuple[str, object]] = []

    async def _send(msg):
        sent.append(msg)

    async def _broadcast(msg_type, msg):
        seen.append((msg_type, msg))

    monkeypatch.setattr(sip, "send", _send)
    monkeypatch.setattr(sip, "_broadcast", _broadcast)

    async def _scenario():
        monkeypatch.setattr(sip, "incoming_requests", asyncio.Queue())
        await sip.incoming_requests.put(raw)
        await sip._process_one_request()

    run(_scenario())
    return sent, seen


def test_an_incoming_message_is_answered_and_broadcast_whole(monkeypatch):
    long_body = STATUS_BODY + " " * 300  # longer than the old 200-char cut
    sent, seen = _deliver(monkeypatch, _raw_message(long_body))

    assert sent and sent[0].startswith("SIP/2.0 200 OK")
    assert "CSeq: 7 MESSAGE" in sent[0]
    [(kind, message)] = seen
    assert kind == "message"
    assert message.body == long_body
    assert message.panda == "blue"
    assert message.sender == "sip:60001@example.invalid"


def test_an_incoming_message_without_a_panda_header(monkeypatch):
    _sent, [(_kind, message)] = _deliver(
        monkeypatch, _raw_message("NEW_PHONEBOOK;abc;21", panda=None))
    assert message.panda is None


def test_an_incoming_message_is_broadcast_even_if_the_answer_fails(monkeypatch):
    seen: list = []

    async def _raises(_msg):
        raise ConnectionResetError

    async def _broadcast(msg_type, msg):
        seen.append(msg)

    monkeypatch.setattr(sip, "send", _raises)
    monkeypatch.setattr(sip, "_broadcast", _broadcast)

    async def _scenario():
        with pytest.raises(ConnectionResetError):
            await sip.handle_incoming_message(
                sip.parse_message(_raw_message(STATUS_BODY)))

    run(_scenario())
    assert len(seen) == 1


def test_the_phonebook_token_never_reaches_the_log(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _sent, [(_kind, message)] = _deliver(monkeypatch, _raw_message(STATUS_BODY))
    assert "secret-token" not in caplog.text
    assert "secret-token" not in repr(message)
    assert "init_status_reply" in caplog.text
