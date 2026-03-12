"""Vimar Intercom Hub — manages SIP + media lifecycle."""

import asyncio
import logging
from collections.abc import Callable

from . import sip_client as sip
from . import media_handler as media
from . import push_sender

_LOGGER = logging.getLogger(__name__)

STREAM_HANGUP_DELAY = 30
MAX_CALL_DURATION = 300  # 5 minutes — auto-hangup safety net


class VimarIntercomHub:
    """Orchestrates SIP registration, calls, door control, and media."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._ring_callbacks: list[Callable] = []
        self._state_callbacks: list[Callable] = []
        self._ws_broadcast_fn: Callable | None = None
        self._has_ws_clients: Callable | None = None
        self._stream_viewers = 0
        self._hangup_task: asyncio.Task | None = None
        self._call_timeout_task: asyncio.Task | None = None
        self._keyframe_task: asyncio.Task | None = None
        self._auto_called = False
        self._auto_call_target: str | None = None

    def set_ws_broadcast(self, fn: Callable):
        self._ws_broadcast_fn = fn

    @property
    def registered(self) -> bool:
        return sip.registered

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
        # Notify WS clients of state change
        if self._ws_broadcast_fn:
            import asyncio
            asyncio.ensure_future(self._ws_broadcast_fn({
                "type": "state",
                "registered": sip.registered,
                "in_call": sip.in_call,
            }))

    async def stream_opened(self, target: str | None = None):
        self._stream_viewers += 1
        _LOGGER.info("Stream opened (%d viewers, target=%s)", self._stream_viewers, target)

        if self._hangup_task:
            self._hangup_task.cancel()
            self._hangup_task = None

        if sip.in_call or sip.calling:
            return

        # Don't auto-call when iOS app WS clients are connected —
        # the app sends the call action explicitly via WebSocket.
        if self._has_ws_clients and self._has_ws_clients():
            _LOGGER.info("Stream opened but WS clients connected — skipping auto-call")
            return

        if sip.registered:
            self._auto_called = True
            self._auto_call_target = target
            # Fire auto-call as background task — don't block the HTTP response
            asyncio.create_task(self._do_auto_call(target))

    async def _do_auto_call(self, target: str | None):
        """Background auto-call when video stream opens without active call."""
        try:
            if target:
                uri = f"sip:{target}@{sip.C.SIP_DOMAIN}"
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

        sip.init(self._handle_broadcast)
        sip.set_state_callback(self._on_sip_state_change)
        media.init(self._handle_broadcast)

        sip.MY_IP = sip.get_local_ip()
        sip.incoming_requests = asyncio.Queue()
        _LOGGER.info("Local IP: %s", sip.MY_IP)

        await media.setup_transports()
        _LOGGER.info("RTP transports ready")

        await sip.connect()

        self._tasks.append(asyncio.create_task(sip.reader_task()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
        self._tasks.append(asyncio.create_task(self._auto_startup()))
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))
        self._running = True

    async def async_stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
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

    async def async_call(self, target: str | None = None) -> tuple[bool, str]:
        self._auto_called = False
        if target:
            uri = f"sip:{target}@{sip.C.SIP_DOMAIN}"
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

    async def async_door(self, target: str | None = None, command: str | None = None) -> tuple[bool, str]:
        """Open door via SIP MESSAGE to targa (PE) address.

        From Tab5S rubrica ACTUATOR_LIST:
          55001 (targa master)  → OPEN_2F = Portone Esterno
          55002 (targa interna) → OPEN_2F = Portone Interno
        The targa forwards the command to its local relay.
        No active call required.
        """
        if target:
            uri = f"sip:{target}@{sip.C.SIP_DOMAIN}"
            body = command or sip.C.DOOR_COMMAND
        else:
            uri = sip.C.DOOR_ESTERNO
            body = sip.C.DOOR_COMMAND

        _LOGGER.info("Door command: uri=%s body=%s registered=%s", uri, body, sip.registered)

        ok, msg = await sip.do_system_message(
            uri, body, extra_headers={"Panda": "command"})

        if ok:
            _LOGGER.info("Door open OK: %s", msg)
            return ok, msg

        # Retry once after re-registration — handles stale connection
        _LOGGER.warning("Door command failed (%s), retrying after re-register...", msg)
        try:
            reg_ok = await sip.do_register()
            if reg_ok:
                ok2, msg2 = await sip.do_system_message(
                    uri, body, extra_headers={"Panda": "command"})
                if ok2:
                    _LOGGER.info("Door open OK on retry: %s", msg2)
                    return ok2, msg2
                _LOGGER.error("Door retry also failed: %s", msg2)
                return ok2, msg2
            else:
                _LOGGER.error("Re-registration failed, cannot retry door")
                return False, "Re-registrazione fallita"
        except Exception as e:
            _LOGGER.error("Door retry error: %s", e)
            return False, str(e)

    async def async_probe(self, target: str) -> tuple[bool, str]:
        uri = f"sip:{target}@{sip.C.SIP_DOMAIN}"
        return await sip.do_options(target=uri)

    async def async_scan(self, start: int, end: int) -> list[dict]:
        results = []
        for addr in range(start, end + 1):
            uri = f"sip:{addr}@{sip.C.SIP_DOMAIN}"
            try:
                ok, msg = await sip.do_options(target=uri)
                results.append({"addr": addr, "ok": ok, "msg": msg})
            except Exception as e:
                results.append({"addr": addr, "ok": False, "msg": str(e)})
            await asyncio.sleep(0.3)
        return results

    async def _handle_broadcast(self, msg_type, msg):
        _LOGGER.debug("[%s] %s", msg_type, msg)

        if msg_type in ("ring", "ring_ended", "call_started", "call_ended", "registered", "error"):
            # Don't broadcast "ring" to WS clients if we initiated the call
            if msg_type == "ring" and (self._auto_called or sip.in_call or sip.calling):
                pass  # Will be handled below (suppress + decline)
            elif self._ws_broadcast_fn:
                try:
                    payload = {
                        "type": msg_type, "msg": msg,
                        "registered": sip.registered, "in_call": sip.in_call,
                    }
                    # Include caller URI so clients can identify which panel is ringing
                    if msg_type == "ring" and sip.pending_incoming.get("caller_uri"):
                        payload["caller_uri"] = sip.pending_incoming["caller_uri"]
                    await self._ws_broadcast_fn(payload)
                except Exception:
                    _LOGGER.exception("WS broadcast error")

        if msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()

        if msg_type == "ring":
            # If we initiated the call (tap to view / auto-call), the Tab5S
            # sends an INVITE back to us. Suppress ring + push — this is NOT
            # a doorbell ring, just the PBX echoing our outgoing call.
            if self._auto_called or sip.in_call or sip.calling:
                _LOGGER.info("Suppressing ring — we initiated this call (auto_called=%s, in_call=%s, calling=%s)",
                             self._auto_called, sip.in_call, sip.calling)
                asyncio.create_task(sip.do_decline_incoming())
                return

            for cb in self._ring_callbacks:
                try:
                    cb()
                except Exception:
                    _LOGGER.exception("Ring callback error")

            # Send VoIP push to wake iOS devices
            sender = push_sender.get_sender()
            if sender:
                caller = sip.pending_incoming.get("caller_uri", "55001")
                # Extract SIP user from URI (e.g. "sip:55001@domain" → "55001")
                if "@" in caller:
                    caller = caller.split("@")[0].replace("sip:", "")
                panel = "esterna"  # TODO: detect panel from caller
                asyncio.create_task(sender.send_voip_push(caller=caller, panel=panel))

    async def _auto_startup(self):
        await asyncio.sleep(2)
        try:
            _LOGGER.info("Auto startup: registering SIP...")
            ok = await sip.do_register()
            _LOGGER.info("Auto startup: register result=%s", ok)
            if ok:
                await asyncio.sleep(1)
                try:
                    ok2, msg2 = await sip.do_connect_profiles()
                    _LOGGER.info("connectProfiles: ok=%s msg=%s", ok2, msg2)
                except Exception as e:
                    _LOGGER.error("connectProfiles error: %s", e)
            else:
                _LOGGER.error("SIP registration failed")
        except Exception as e:
            _LOGGER.error("Auto startup error: %s", e, exc_info=True)

    async def _keepalive_loop(self):
        while self._running:
            await asyncio.sleep(120)
            if sip.registered:
                try:
                    ok = await sip.do_register()
                    _LOGGER.debug("Keepalive: %s", "OK" if ok else "FAILED")
                except Exception as e:
                    _LOGGER.error("Keepalive error: %s", e)
