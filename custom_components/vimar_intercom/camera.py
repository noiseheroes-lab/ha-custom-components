"""Camera platform for Vimar Intercom."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)

AV_PATH = "/api/vimar_intercom/av"
SIGNATURE_LIFETIME = timedelta(minutes=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the intercom camera."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomCamera(hub, entry.entry_id)])


class VimarIntercomCamera(Camera):
    """Live video from the entrance panel.

    Opening the stream places a SIP call to the panel, because the panel
    only sends video inside a call. Home Assistant fetches the stream
    over a signed URL, so the underlying view still requires
    authentication.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "intercom"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the camera to the intercom device."""
        super().__init__()
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_streaming(self) -> bool:
        """True while a call with video is up."""
        return self._hub.in_call

    @property
    def is_on(self) -> bool:
        """The camera is always available; the stream starts on demand."""
        return True

    @property
    def use_stream_for_stills(self) -> bool:
        """Take stills from the stream.

        The panel sends H.264, never JPEG, so there is no still image to
        fetch. Without this, Home Assistant would call `camera_image` and
        get NotImplementedError for every snapshot and dashboard preview.
        """
        return True

    async def stream_source(self) -> str | None:
        """Signed URL of the MPEG-TS stream."""
        signed = async_sign_path(self.hass, AV_PATH, SIGNATURE_LIFETIME)
        return f"{get_url(self.hass, prefer_external=False)}{signed}"
