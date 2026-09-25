"""Tests for the SIP layer's module state and its request loop.

sip_client.py imports no Home Assistant module, so it loads through the
stub package tests/conftest.py installs. Its state lives in module
globals that outlive a config entry, which is precisely why these two
behaviours need pinning down.
"""

import asyncio

import pytest

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
