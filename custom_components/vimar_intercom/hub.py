"""Vimar Intercom Hub — manages SIP + media lifecycle."""

import asyncio
import logging
import time
from collections.abc import Callable

from . import sip_client as sip
from . import media_handler as media
from .const import DOOR_COMMAND_CURRENT, EVENT_RING, REGISTRATION_DOWN_GRACE
from .runtime import RuntimeConfig

_LOGGER = logging.getLogger(__name__)

STREAM_HANGUP_DELAY = 30
MAX_CALL_DURATION = 300  # 5 minutes — auto-hangup safety net
# How long an unload waits for the BYE before giving up on it. A reload
# must not be able to block on a socket the peer has stopped answering.
HANGUP_ON_UNLOAD_TIMEOUT = 5
REGISTRATION_WATCHDOG_INTERVAL = 30


class VimarIntercomHub:
    """Orchestrates SIP registration, calls, door control, and media."""

    def __init__(self, cfg: RuntimeConfig) -> None:
        """Store the runtime configuration and initialise the state."""
        self._cfg = cfg
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._ring_callbacks: list[Callable] = []
        self._state_callbacks: list[Callable] = []
        self._stream_viewers = 0
        self._hangup_task: asyncio.Task | None = None
        self._call_timeout_task: asyncio.Task | None = None
        self._keyframe_task: asyncio.Task | None = None
        self._auto_called = False
        self._auto_call_target: str | None = None
        # Fire-and-forget work started from callbacks. Kept so an unload
        # cannot leave an auto-call or a decline running against a SIP
        # layer that has just been reset.
        self._background: set[asyncio.Task] = set()
        self._raise_issue: Callable | None = None
        self._clear_issue: Callable | None = None
        self._hass = None
        self._entry_id = ""

    @property
    def config(self) -> RuntimeConfig:
        """Runtime configuration for this hub."""
        return self._cfg

    def set_hass(self, hass, entry_id: str) -> None:
        """Give the hub the bus it fires ring events on."""
        self._hass = hass
        self._entry_id = entry_id

    def _panel_for(self, caller_uri: str) -> tuple[str, str]:
        """Map an incoming caller URI to a configured panel."""
        address = caller_uri.split("@")[0].removeprefix("sip:")
        for panel in self._cfg.panels:
            if panel.address == address:
                return panel.address, panel.name
        return address, address or "unknown"

    @property
    def registered(self) -> bool:
        return sip.is_registered()

    @property
    def in_call(self) -> bool:
        return sip.in_call

    def register_ring_callback(self, callback: Callable) -> None:
        self._ring_callbacks.append(callback)

    def unregister_ring_callback(self, callback: Callable) -> None:
        if callback in self._ring_callbacks:
            self._ring_callbacks.remove(callback)

    def register_state_callback(self, callback: Callable) -> None:
        """Register a callback for SIP state changes (registered, in_call)."""
        self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: Callable) -> None:
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def _on_sip_state_change(self):
        """Called by sip_client when registered/in_call changes."""
        for cb in self._state_callbacks:
            try:
                cb()
            except Exception:
                _LOGGER.exception("State callback error")

    def set_issue_callbacks(self, raise_issue: Callable, clear_issue: Callable) -> None:
        """Install the callbacks used to raise and clear the repair issue."""
        self._raise_issue = raise_issue
        self._clear_issue = clear_issue

    async def _registration_watchdog(self) -> None:
        """Raise a repair issue when registration stays down too long."""
        down_since: float | None = None
        raised = False
        try:
            while self._running:
                await asyncio.sleep(REGISTRATION_WATCHDOG_INTERVAL)
                if sip.is_registered():
                    if raised and self._clear_issue:
                        self._clear_issue()
                        raised = False
                    down_since = None
                    continue
                if down_since is None:
                    down_since = time.monotonic()
                elif (not raised
                      and time.monotonic() - down_since >= REGISTRATION_DOWN_GRACE
                      and self._raise_issue):
                    self._raise_issue()
                    raised = True
        except asyncio.CancelledError:
            pass

    def _track(self, coro) -> asyncio.Task:
        """Run a coroutine in the background without losing track of it."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _schedule_delayed_hangup(self) -> None:
        """Start the grace period before dropping an unwatched auto-call."""
        if self._hangup_task:
            self._hangup_task.cancel()
        self._hangup_task = asyncio.create_task(self._delayed_hangup())

    def _clear_auto_call(self) -> None:
        """Forget that the live call was one this hub placed itself.

        Every path out of an auto-call comes through here. A stale
        `_auto_called` is not a cosmetic leak: `_handle_broadcast` reads
        it to tell a real doorbell press from the PBX echoing our own
        INVITE, so leaving it set declines every visitor with a 603 and
        fires no ring event until Home Assistant is restarted.
        """
        self._auto_called = False
        self._auto_call_target = None

    async def stream_opened(self, target: str | None = None):
        self._stream_viewers += 1
        _LOGGER.debug("Stream opened (%d viewers)", self._stream_viewers)

        if self._hangup_task:
            self._hangup_task.cancel()
            self._hangup_task = None

        # `_auto_called` is part of the guard, not just a record: it is
        # set synchronously here, before any await, so a second viewer
        # arriving before the first auto-call task has even run sees a
        # call is already being placed. Without it both viewers call
        # `do_call`, the loser gets "Already in a call" and clears the
        # flag mid-call, and the echo INVITE becomes a phantom ring.
        if sip.in_call or sip.calling or self._auto_called:
            return

        if not sip.is_registered():
            return

        self._auto_called = True
        self._auto_call_target = target
        # Fire in the background — don't block the HTTP response.
        self._track(self._do_auto_call(target))

    async def _do_auto_call(self, target: str | None):
        """Answer or place the call a newly opened stream needs.

        A panel that is ringing us right now already has an INVITE
        pending: answering it is what the user meant. Placing a second,
        outgoing INVITE to the default panel instead — which is what
        this used to do — is a call collision, and it is exactly what
        the README's own "snapshot on ring" automation triggers.
        """
        try:
            if sip.pending_incoming["active"]:
                ok, msg = await sip.do_answer_incoming()
            elif target:
                ok, msg = await sip.do_call(target=self._cfg.panel_uri(target))
            else:
                ok, msg = await sip.do_call()
            if not ok:
                _LOGGER.error("Auto-call failed: %s", msg)
                self._clear_auto_call()
            elif self._stream_viewers == 0:
                # The viewer gave up while the INVITE was still in
                # flight — the HTTP view waits 15 s, `do_call` allows
                # 45 s. Without this the call establishes with nobody
                # watching and runs to MAX_CALL_DURATION, holding the
                # account's single registration the whole time.
                self._schedule_delayed_hangup()
        except asyncio.CancelledError:
            self._clear_auto_call()
            raise
        except Exception as e:  # noqa: BLE001 - reported, never fatal
            _LOGGER.error("Auto-call error: %s", e)
            self._clear_auto_call()

    async def stream_closed(self):
        self._stream_viewers = max(0, self._stream_viewers - 1)
        _LOGGER.debug("Stream viewer disconnected (%d remaining)",
                      self._stream_viewers)

        if self._stream_viewers or not self._auto_called:
            return

        if sip.in_call:
            self._schedule_delayed_hangup()
        elif not sip.calling:
            # There is no call to hang up and none on its way: the
            # auto-call never established (the view gave up waiting), or
            # the panel ended it first. Nothing else will ever arrive to
            # clear the flag, so clear it here.
            self._clear_auto_call()

    async def _hangup_and_clear(self, hang_up: bool, reason: str) -> None:
        """End the call this hub placed, then forget it — whatever happens.

        `sip.send` raises whenever the SIP socket has gone, and that is
        exactly the state these timers fire in during an outage. An
        exception escaping here would kill the task before it reached
        `_clear_auto_call`, and a stale `_auto_called` declines every
        later visitor with a 603 — the very wedge that flag's single
        reset point exists to prevent.
        """
        try:
            if hang_up and sip.in_call:
                _LOGGER.info("%s; hanging up", reason)
                await sip.do_hangup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a BYE we cannot send is not fatal
            _LOGGER.debug("Hang-up after %s failed", reason, exc_info=True)
        finally:
            self._clear_auto_call()

    async def _delayed_hangup(self):
        try:
            await asyncio.sleep(STREAM_HANGUP_DELAY)
        except asyncio.CancelledError:
            # A viewer came back inside the grace period. The call stays
            # up and stays ours, so the flag stays set.
            return
        await self._hangup_and_clear(
            self._stream_viewers == 0 and self._auto_called,
            "no viewers left on the call this hub placed")

    def _start_call_timeout(self):
        """Start max call duration timer."""
        self._cancel_call_timeout()
        self._call_timeout_task = asyncio.create_task(self._call_timeout())

    def _cancel_call_timeout(self):
        if self._call_timeout_task:
            self._call_timeout_task.cancel()
            self._call_timeout_task = None

    async def _call_timeout(self):
        try:
            await asyncio.sleep(MAX_CALL_DURATION)
        except asyncio.CancelledError:
            # The call ended on its own; `call_ended` has already cleared
            # the flag.
            return
        await self._hangup_and_clear(
            True, f"the maximum call duration ({MAX_CALL_DURATION}s) was reached")

    def _start_keyframe_loop(self):
        """Send periodic keyframe requests during calls for video recovery."""
        self._cancel_keyframe_loop()
        self._keyframe_task = asyncio.create_task(self._keyframe_loop())

    def _cancel_keyframe_loop(self):
        if self._keyframe_task:
            self._keyframe_task.cancel()
            self._keyframe_task = None

    async def _keyframe_loop(self):
        """Aggressive keyframe bursts at start, then periodic requests."""
        try:
            # Immediate first request — no delay
            if sip.in_call:
                await sip.send_keyframe_request()
            # Rapid burst: 8 requests at 100ms intervals
            for i in range(8):
                await asyncio.sleep(0.1)
                if not sip.in_call:
                    return
                await sip.send_keyframe_request()
            # Then periodic for the rest of the call, whether or not a
            # viewer is attached: the registry caches every IDR that
            # arrives, and that cache is what a still is decoded from,
            # so stopping the requests would freeze the snapshot of a
            # call answered without the camera open.
            while sip.in_call:
                await asyncio.sleep(2)
                await sip.send_keyframe_request()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - one failed INFO is not fatal
            # Without this the task dies on the first send failure with
            # "Task exception was never retrieved" and the call gets no
            # further keyframe requests, so video never recovers.
            _LOGGER.debug("Keyframe request loop stopped early", exc_info=True)

    async def async_start(self):
        if self._running:
            return

        # The SIP and media layers keep their state in module globals,
        # which survive an entry reload. Start from a known-clean slate.
        sip.reset_state()

        sip.configure(self._cfg)
        media.configure(self._cfg)

        sip.init(self._handle_broadcast)
        sip.set_state_callback(self._on_sip_state_change)
        media.init(self._handle_broadcast)

        sip.MY_IP = sip.get_local_ip()
        sip.incoming_requests = asyncio.Queue()
        _LOGGER.info("Local IP: %s", sip.MY_IP)

        await media.setup_transports()
        _LOGGER.info("RTP transports ready")

        # The supervisor makes its own first connection attempt; connecting
        # here too would leak that first socket the moment the supervisor
        # opens its own.
        self._tasks.append(asyncio.create_task(sip.connection_supervisor()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
        self._tasks.append(asyncio.create_task(self._registration_watchdog()))
        self._running = True

    async def async_stop(self):
        self._running = False

        # Hang up before tearing the tasks down: after this the socket
        # is gone and no BYE can be sent, leaving the panel holding a
        # call it thinks is live and the account's single registration
        # occupied.
        #
        # Bounded, because nothing inside the hang-up is. `sip.send`
        # takes the connection lock and then awaits `writer.drain()`,
        # and on a half-open TCP connection — the socket is not closed,
        # the peer simply stops acknowledging — the reader loop's CRLF
        # keepalive is already blocked in its own drain while holding
        # that lock. The reader task is still alive, since this runs
        # before the cancel loop below, so the wait would never end and
        # the entry would sit in "unloading" forever: an option change
        # mid-call never reloads, and a Home Assistant restart hangs.
        if sip.in_call:
            try:
                await asyncio.wait_for(sip.do_hangup(), HANGUP_ON_UNLOAD_TIMEOUT)
            except Exception:  # noqa: BLE001 - shutdown must not fail here
                _LOGGER.debug("Hang-up on unload failed", exc_info=True)

        for t in list(self._background):
            t.cancel()
        self._background.clear()

        for t in self._tasks:
            t.cancel()
        if self._tasks:
            # Await teardown so no task (notably the registration watchdog,
            # which would otherwise sleep up to REGISTRATION_WATCHDOG_INTERVAL
            # seconds after self._running flips) outlives an entry reload.
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._hangup_task:
            self._hangup_task.cancel()
        self._cancel_call_timeout()
        self._cancel_keyframe_loop()
        await media.stop_media()
        media.close_transports()
        if sip.writer:
            try:
                sip.writer.close()
            except Exception:  # noqa: BLE001 - closing a dead socket may raise
                pass
        self._clear_auto_call()
        self._stream_viewers = 0
        _LOGGER.info("Hub stopped")

    async def async_reconnect(self) -> None:
        """Force the SIP connection to be rebuilt."""
        _LOGGER.info("Manual reconnect requested")
        sip.request_reconnect()

    async def async_call(self, target: str | None = None) -> tuple[bool, str]:
        self._clear_auto_call()
        if target:
            uri = self._cfg.panel_uri(target)
            return await sip.do_call(target=uri)
        return await sip.do_call()

    async def async_answer(self) -> tuple[bool, str]:
        return await sip.do_answer_incoming()

    async def async_decline(self):
        await sip.do_decline_incoming()

    async def async_hangup(self):
        self._clear_auto_call()
        self._cancel_call_timeout()
        await sip.do_hangup()

    async def async_door(
        self, target: str | None = None, command: str | None = None
    ) -> tuple[bool, str]:
        """Open a door, the way the Vimar app itself does.

        Three cases:

        - An explicit target: OPEN_CURRENT to that panel, for plants with
          more than one entrance.
        - During a call: OPEN_CURRENT to the door relay group, which opens
          the relay of whichever panel is calling.
        - Otherwise: the configured door command, OPEN_2F by default, to
          the door relay group — the main entrance.

        The relay group comes from the QR, so the common case needs no
        configuration.
        """
        if target:
            uri = self._cfg.panel_uri(target)
            body = command or DOOR_COMMAND_CURRENT
        elif sip.in_call:
            uri = self._cfg.door_uri
            body = command or DOOR_COMMAND_CURRENT
        else:
            uri = self._cfg.door_uri
            body = command or self._cfg.door_command

        _LOGGER.debug("Door command %s to %s (registered=%s)", body, uri, sip.is_registered())

        ok, msg = await sip.do_system_message(
            uri, body, extra_headers={"Panda": "command"})
        if ok:
            _LOGGER.info("Door %s opened", uri)
            return True, msg

        if not sip.is_registered():
            # Re-registering from here would call `connect()` outside the
            # supervisor, opening a TLS socket whose responses nobody
            # reads — one leaked connection per door press for as long as
            # the outage lasts, and a 15 s wait before failing anyway.
            _LOGGER.warning("Door command to %s failed (%s); not registered, "
                            "asking the supervisor to reconnect", uri, msg)
            sip.request_reconnect()
            return False, ("Not connected to the intercom. A reconnect has "
                           "been requested; try again in a few seconds.")

        _LOGGER.warning("Door command to %s failed (%s); retrying once", uri, msg)
        try:
            ok, msg = await sip.do_system_message(
                uri, body, extra_headers={"Panda": "command"})
            if ok:
                _LOGGER.info("Door %s opened on retry", uri)
            else:
                _LOGGER.error("Door %s failed on retry: %s", uri, msg)
            return ok, msg
        except Exception as err:  # noqa: BLE001 - surfaced to the caller
            _LOGGER.error("Door retry error: %s", err)
            return False, str(err)

    async def _handle_broadcast(self, msg_type, msg):
        """React to a SIP-layer event."""
        _LOGGER.debug("[%s] %s", msg_type, msg)

        if msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()
            # The panel, the cloud or our own BYE ended the call. This is
            # the one place every ending converges, so it is where the
            # auto-call record is torn down.
            self._clear_auto_call()

        if msg_type == "ring":
            # When we placed the call ourselves the panel INVITEs us back.
            # That is the PBX echoing our own call, not a doorbell press.
            if self._auto_called or sip.in_call or sip.calling:
                _LOGGER.debug(
                    "Suppressing ring: call initiated locally "
                    "(auto_called=%s, in_call=%s, calling=%s)",
                    self._auto_called, sip.in_call, sip.calling)
                self._track(sip.do_decline_incoming())
                return

            address, name = self._panel_for(
                sip.pending_incoming.get("caller_uri", ""))

            if self._hass is not None:
                self._hass.bus.async_fire(EVENT_RING, {
                    "panel": address,
                    "panel_name": name,
                    "entry_id": self._entry_id,
                })

            for cb in self._ring_callbacks:
                try:
                    cb(address)
                except Exception:
                    _LOGGER.exception("Ring callback error")
