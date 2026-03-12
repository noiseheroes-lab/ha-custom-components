"""Camera platform for Vimar Intercom."""

from __future__ import annotations

import logging

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomCamera(hub, entry.entry_id, hass)])


class VimarIntercomCamera(Camera):
    """Intercom camera — streams video from SIP/RTP pipeline.

    Uses MJPEG directly (no RTSP/WebRTC). When the stream is opened
    (e.g. from Apple Home), the hub auto-calls the intercom.
    """

    _attr_has_entity_name = False
    _attr_name = "Intercom"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, hub, entry_id: str, hass: HomeAssistant) -> None:
        super().__init__()
        self._hub = hub
        self._hass = hass
        self._attr_unique_id = f"{entry_id}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_streaming(self) -> bool:
        """True when there's an active SIP call with video."""
        return self._hub.in_call

    @property
    def is_on(self) -> bool:
        return True

    @property
    def frontend_stream_type(self):
        """Tell HA frontend to use MJPEG."""
        from homeassistant.components.camera import StreamType
        return StreamType.MJPEG

    async def stream_source(self) -> str | None:
        """AV stream URL for HomeKit (MPEG-TS with H264 video + PCMU audio)."""
        base = self._hass.config.internal_url or "http://127.0.0.1:8123"
        return f"{base}/api/vimar_intercom/av"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest cached JPEG frame (no auto-call).

        This is called by Apple Home for the thumbnail on the home screen.
        We only return whatever frame we already have — no SIP call triggered.
        The live stream (user taps camera) goes through stream_source/MJPEG view
        which triggers auto-call there.
        """
        return self._hub.video_frame

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Serve MJPEG stream directly to the HA frontend.

        Does NOT auto-call — only shows video if a call is already active.
        Use the Call button to start a call first.
        """
        import asyncio

        response = web.StreamResponse()
        response.content_type = "multipart/x-mixed-replace; boundary=frame"
        await response.prepare(request)
        try:
            while True:
                frame = self._hub.video_frame
                if frame:
                    await response.write(
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame + b"\r\n"
                    )
                await asyncio.sleep(0.1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response
