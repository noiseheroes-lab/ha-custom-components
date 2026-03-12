"""Vimar Intercom integration for Home Assistant."""

import asyncio
import json
import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .hub import VimarIntercomHub
from . import media_handler as media
from . import push_sender
from . import sip_client as sip

_LOGGER = logging.getLogger(__name__)

# Ring buffer for debug logs
_debug_log: list[str] = []
_MAX_DEBUG_LOG = 200


class _DebugHandler(logging.Handler):
    """Captures vimar_intercom logs into a ring buffer."""
    def emit(self, record):
        try:
            msg = self.format(record)
            _debug_log.append(msg)
            if len(_debug_log) > _MAX_DEBUG_LOG:
                del _debug_log[:len(_debug_log) - _MAX_DEBUG_LOG]
        except Exception:
            pass


# Attach debug handler to all vimar loggers
_dh = _DebugHandler()
_dh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
logging.getLogger("custom_components.vimar_intercom").addHandler(_dh)
logging.getLogger("custom_components.vimar_intercom").setLevel(logging.DEBUG)
PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor"]

# Active audio WebSocket clients
_audio_ws_clients: set[web.WebSocketResponse] = set()
_hub_ref: VimarIntercomHub | None = None


async def _ws_send_bytes_to_clients(data: bytes):
    """Send binary audio data to all connected iOS/web audio clients."""
    dead = set()
    for ws in _audio_ws_clients:
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.add(ws)
    _audio_ws_clients.difference_update(dead)


async def _broadcast_text(data: dict):
    """Send JSON text message to all audio WS clients."""
    text = json.dumps(data)
    dead = set()
    for ws in _audio_ws_clients:
        try:
            await ws.send_str(text)
        except Exception:
            dead.add(ws)
    _audio_ws_clients.difference_update(dead)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    global _hub_ref
    hub = VimarIntercomHub()
    _hub_ref = hub

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub}

    await hub.async_start()

    # Wire up audio broadcast to WebSocket clients
    media.ws_send_bytes = _ws_send_bytes_to_clients
    hub.set_ws_broadcast(_broadcast_text)
    hub._has_ws_clients = lambda: len(_audio_ws_clients) > 0

    # Initialize APNs VoIP push sender
    from .const import APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_BUNDLE_ID, APNS_SANDBOX
    if APNS_KEY_ID and APNS_TEAM_ID:
        push_sender.init(APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_BUNDLE_ID, APNS_SANDBOX)
        _LOGGER.info("APNs VoIP push sender initialized")
    else:
        _LOGGER.warning("APNs push not configured — set APNS_KEY_ID and APNS_TEAM_ID in const.py")

    hass.http.register_view(VimarMjpegView(hub))
    hass.http.register_view(VimarAVStreamView(hub))
    hass.http.register_view(VimarAudioWSView(hub))
    hass.http.register_view(VimarPushTokenView())
    hass.http.register_view(VimarDebugView())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    global _hub_ref
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["hub"].async_stop()
        _hub_ref = None
    return ok


class VimarAudioWSView(HomeAssistantView):
    """WebSocket endpoint for bidirectional audio + intercom control.

    Binary messages:
      Server → Client: 0x01 + PCM16LE (intercom audio, 8kHz mono)
      Client → Server: 0x02 + PCM16LE (mic audio, 8kHz mono)

    Text messages (JSON):
      Client → Server: {"action": "call"|"hangup"|"door"|"register"|"status"}
      Server → Client: {"type": "state"|"call_started"|"call_ended"|"ring"|"door"|"error", ...}
    """

    url = "/api/vimar_intercom/audio_ws"
    name = "api:vimar_intercom:audio_ws"
    requires_auth = False

    def __init__(self, hub: VimarIntercomHub):
        self._hub = hub

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        _audio_ws_clients.add(ws)
        _LOGGER.info("Audio WS client connected (%d total)", len(_audio_ws_clients))

        # Send initial state
        await ws.send_str(json.dumps({
            "type": "state",
            "registered": self._hub.registered,
            "in_call": self._hub.in_call,
        }))

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._handle_text(ws, msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    # Client sending mic audio: 0x02 prefix + PCM16LE
                    if len(msg.data) > 1 and msg.data[0] == 0x02 and self._hub.in_call:
                        media.send_audio(msg.data[1:])
                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        except Exception as e:
            _LOGGER.error("Audio WS error: %s", e)
        finally:
            _audio_ws_clients.discard(ws)
            _LOGGER.info("Audio WS client disconnected (%d remaining)", len(_audio_ws_clients))

        return ws

    async def _handle_text(self, ws: web.WebSocketResponse, text: str):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        action = data.get("action")
        _LOGGER.info("WS action received: %s (data=%s)", action, data)
        hub = self._hub

        if action == "status":
            await ws.send_str(json.dumps({
                "type": "state",
                "registered": hub.registered,
                "in_call": hub.in_call,
            }))

        elif action == "call":
            target = data.get("target")  # optional: "55002" etc.
            try:
                ok, m = await hub.async_call(target=target)
                if ok:
                    await _broadcast_text({"type": "call_started", "msg": m,
                                           "target": target,
                                           "registered": hub.registered, "in_call": True})
                elif sip.in_call:
                    # Already connected — tell the app immediately
                    _LOGGER.info("Call request: already in call, notifying client")
                    await _broadcast_text({"type": "call_started", "msg": "Already in call",
                                           "target": target,
                                           "registered": hub.registered, "in_call": True})
                elif sip.calling:
                    # Call in progress (connecting) — SIP broadcast will notify when connected
                    _LOGGER.info("Call request: already calling, will notify on connect")
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "hangup":
            try:
                await hub.async_hangup()
                await _broadcast_text({"type": "call_ended", "msg": "Call ended",
                                       "registered": hub.registered, "in_call": False})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "switch":
            # Atomic panel switch: BYE current + INVITE new (like official app)
            target = data.get("target")
            if not target:
                await ws.send_str(json.dumps({"type": "error", "msg": "No target"}))
            else:
                try:
                    # Suppress broadcast during switch — do hangup silently
                    sip._suppress_broadcast = True
                    await hub.async_hangup()
                    sip._suppress_broadcast = False
                    await asyncio.sleep(0.05)  # Minimal — just enough for BYE to send
                    ok, m = await hub.async_call(target=target)
                    if ok:
                        await _broadcast_text({"type": "call_started", "msg": m,
                                               "target": target,
                                               "registered": hub.registered, "in_call": True})
                    else:
                        await _broadcast_text({"type": "call_ended", "msg": f"Switch failed: {m}",
                                               "registered": hub.registered, "in_call": False})
                except Exception as e:
                    sip._suppress_broadcast = False
                    await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "door":
            target = data.get("target")  # "55001" (esterno) or "55002" (interno)
            _LOGGER.info("Door action: target=%s", target)
            try:
                ok, m = await hub.async_door(target=target)
                t = "door" if ok else "error"
                await _broadcast_text({"type": t, "msg": m})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "register":
            try:
                ok = await sip.do_register()
                if ok:
                    await _broadcast_text({"type": "registered", "msg": "SIP registered",
                                           "registered": True, "in_call": hub.in_call})
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": "Registration failed"}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "probe":
            target = data.get("target", "")
            try:
                ok, m = await hub.async_probe(target)
                await ws.send_str(json.dumps({"type": "probe_result",
                                               "target": target, "ok": ok, "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "scan":
            start = data.get("start", 55001)
            end = data.get("end", 55020)
            try:
                results = await hub.async_scan(start, end)
                await ws.send_str(json.dumps({"type": "scan_result", "results": results}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "answer":
            try:
                ok, m = await hub.async_answer()
                if ok:
                    # Broadcast ring_ended FIRST so other devices stop ringing
                    await _broadcast_text({"type": "ring_ended", "msg": "Answered on another device",
                                           "registered": hub.registered, "in_call": True})
                    await _broadcast_text({"type": "call_started", "msg": m,
                                           "registered": hub.registered, "in_call": True})
                else:
                    await ws.send_str(json.dumps({"type": "error", "msg": m}))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "decline":
            try:
                await hub.async_decline()
                await _broadcast_text({"type": "ring_ended", "msg": "Declined",
                                       "registered": hub.registered, "in_call": False})
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))

        elif action == "reconnect":
            _LOGGER.info("Force reconnect requested via WS")
            try:
                ok = await sip.reconnect()
                await ws.send_str(json.dumps({
                    "type": "state",
                    "registered": hub.registered,
                    "in_call": hub.in_call,
                    "msg": "Reconnected" if ok else "Reconnect failed",
                }))
            except Exception as e:
                await ws.send_str(json.dumps({"type": "error", "msg": str(e)}))


class VimarPushTokenView(HomeAssistantView):
    """REST endpoint for iOS app to register/unregister VoIP push tokens."""

    url = "/api/vimar_intercom/push_token"
    name = "api:vimar_intercom:push_token"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        token = data.get("token")
        if not token:
            return web.json_response({"error": "Missing token"}, status=400)

        sender = push_sender.get_sender()
        if not sender:
            return web.json_response({"error": "Push not configured"}, status=503)

        device_name = data.get("device_name", "unknown")
        sender.register_token(token, device_name)
        return web.json_response({"status": "ok", "devices": len(sender.registered_devices)})

    async def delete(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        token = data.get("token")
        if not token:
            return web.json_response({"error": "Missing token"}, status=400)

        sender = push_sender.get_sender()
        if sender:
            sender.unregister_token(token)
        return web.json_response({"status": "ok"})


class VimarDebugView(HomeAssistantView):
    """Debug endpoint — returns recent vimar_intercom logs as plain text."""

    url = "/api/vimar_intercom/debug"
    name = "api:vimar_intercom:debug"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        try:
            n = int(request.query.get("lines", "100"))
        except ValueError:
            n = 100
        text = "\n".join(_debug_log[-n:])
        return web.Response(text=text, content_type="text/plain")


class VimarMjpegView(HomeAssistantView):
    """Serve MJPEG stream at /api/vimar_intercom/video."""

    url = "/api/vimar_intercom/video"
    name = "api:vimar_intercom:video"
    requires_auth = False

    def __init__(self, hub: VimarIntercomHub):
        self._hub = hub

    async def get(self, request: web.Request) -> web.StreamResponse:
        target = request.query.get("target")
        await self._hub.stream_opened(target=target)

        response = web.StreamResponse()
        response.content_type = "multipart/x-mixed-replace; boundary=frame"
        await response.prepare(request)
        try:
            waited = 0
            while not self._hub.video_frame and waited < 25:
                await asyncio.sleep(0.5)
                waited += 0.5

            while True:
                frame = self._hub.video_frame
                if frame:
                    await response.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame + b"\r\n"
                    )
                await asyncio.sleep(0.04)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await self._hub.stream_closed()
        return response


class VimarAVStreamView(HomeAssistantView):
    """Serve MPEG-TS stream (H264 video + PCMU audio) at /api/vimar_intercom/av."""

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    requires_auth = False

    def __init__(self, hub: VimarIntercomHub):
        self._hub = hub

    async def get(self, request: web.Request) -> web.StreamResponse:
        _LOGGER.info("AV stream requested — triggering auto-call")
        await self._hub.stream_opened()

        waited = 0
        while not self._hub.in_call and waited < 15:
            await asyncio.sleep(0.5)
            waited += 0.5

        if not self._hub.in_call:
            _LOGGER.warning("AV stream: call not established after 15s")
            await self._hub.stream_closed()
            return web.Response(status=503, text="Call not established")

        await media.start_av_ffmpeg()
        if not media.av_ffmpeg_proc:
            await self._hub.stream_closed()
            return web.Response(status=503, text="ffmpeg failed to start")

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        await response.prepare(request)

        loop = asyncio.get_event_loop()
        try:
            while media.av_ffmpeg_proc and media.av_ffmpeg_proc.poll() is None:
                chunk = await loop.run_in_executor(
                    None, media.av_ffmpeg_proc.stdout.read, 4096)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await media.stop_av_ffmpeg()
            await self._hub.stream_closed()
        return response
