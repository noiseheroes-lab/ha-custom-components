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

    @property
    def is_ringing(self) -> bool:
        return sip.pending_incoming["active"]

    @property
    def video_frame(self) -> bytes | None:
        return None  # Video sent directly via WebSocket H.264 NALs

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

    async def stream_opened(self, target: str | None = None):
        self._stream_viewers += 1
        _LOGGER.debug("Stream opened (%d viewers)", self._stream_viewers)

        if self._hangup_task:
            self._hangup_task.cancel()
            self._hangup_task = None

        if sip.in_call or sip.calling:
            return

        if sip.is_registered():
            self._auto_called = True
            self._auto_call_target = target
            # Fire auto-call as background task — don't block the HTTP response
            asyncio.create_task(self._do_auto_call(target))

    async def _do_auto_call(self, target: str | None):
        """Background auto-call when video stream opens without active call."""
        try:
            if target:
                uri = self._cfg.panel_uri(target)
                ok, msg = await sip.do_call(target=uri)
            else:
                ok, msg = await sip.do_call()
            if not ok:
                _LOGGER.error("Auto-call failed: %s", msg)
                self._auto_called = False
        except Exception as e:
            _LOGGER.error("Auto-call error: %s", e)
            self._auto_called = False

    async def stream_closed(self):
        self._stream_viewers = max(0, self._stream_viewers - 1)
        _LOGGER.info("Stream viewer disconnected (%d remaining)", self._stream_viewers)

        if self._stream_viewers == 0 and self._auto_called and sip.in_call:
            self._hangup_task = asyncio.create_task(self._delayed_hangup())

    async def _delayed_hangup(self):
        try:
            await asyncio.sleep(STREAM_HANGUP_DELAY)
            if self._stream_viewers == 0 and self._auto_called and sip.in_call:
                _LOGGER.info("No viewers, hanging up auto-call")
                await sip.do_hangup()
                self._auto_called = False
        except asyncio.CancelledError:
            pass

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
            if sip.in_call:
                _LOGGER.info("Max call duration (%ds) reached, hanging up", MAX_CALL_DURATION)
                await sip.do_hangup()
                self._auto_called = False
        except asyncio.CancelledError:
            pass

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
            # Then periodic every 2s
            while sip.in_call:
                await asyncio.sleep(2)
                await sip.send_keyframe_request()
        except asyncio.CancelledError:
            pass

    async def async_start(self):
        if self._running:
            return

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
            except Exception:
                pass
        _LOGGER.info("Hub stopped")

    async def async_reconnect(self) -> None:
        """Force the SIP connection to be rebuilt."""
        _LOGGER.info("Manual reconnect requested")
        sip.request_reconnect()

    async def async_call(self, target: str | None = None) -> tuple[bool, str]:
        self._auto_called = False
        if target:
            uri = self._cfg.panel_uri(target)
            return await sip.do_call(target=uri)
        return await sip.do_call()

    async def async_answer(self) -> tuple[bool, str]:
        return await sip.do_answer_incoming()

    async def async_decline(self):
        await sip.do_decline_incoming()

    async def async_hangup(self):
        self._auto_called = False
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

        _LOGGER.warning("Door command to %s failed (%s); re-registering and retrying",
                        uri, msg)
        try:
            if not await sip.do_register():
                return False, "Re-registration failed"
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

    async def async_probe(self, target: str) -> tuple[bool, str]:
        uri = self._cfg.panel_uri(target)
        return await sip.do_options(target=uri)

    async def async_scan(self, start: int, end: int) -> list[dict]:
        results = []
        for addr in range(start, end + 1):
            uri = self._cfg.panel_uri(str(addr))
            try:
                ok, msg = await sip.do_options(target=uri)
                results.append({"addr": addr, "ok": ok, "msg": msg})
            except Exception as e:
                results.append({"addr": addr, "ok": False, "msg": str(e)})
            await asyncio.sleep(0.3)
        return results

    async def _handle_broadcast(self, msg_type, msg):
        """React to a SIP-layer event."""
        _LOGGER.debug("[%s] %s", msg_type, msg)

        if msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()

        if msg_type == "ring":
            # When we placed the call ourselves the panel INVITEs us back.
            # That is the PBX echoing our own call, not a doorbell press.
            if self._auto_called or sip.in_call or sip.calling:
                _LOGGER.debug(
                    "Suppressing ring: call initiated locally "
                    "(auto_called=%s, in_call=%s, calling=%s)",
                    self._auto_called, sip.in_call, sip.calling)
                asyncio.create_task(sip.do_decline_incoming())
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
