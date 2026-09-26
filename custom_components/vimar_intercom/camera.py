"""Camera platform for Vimar Intercom."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from . import media_handler as media
from .const import (
    ATTR_DEFAULT_PANEL,
    ATTR_DEFAULT_PANEL_NAME,
    ATTR_INTERCOM_ROLE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    ROLE_CAMERA,
)
from .hub import MAX_CALL_DURATION

AV_PATH = "/api/vimar_intercom/av"

# Comfortably longer than MAX_CALL_DURATION. A stream still running when
# the signature expires cannot renew it, and the restart Home
# Assistant's `stream` component attempts would get a 401 it has no way
# to recover from.
SIGNATURE_LIFETIME = timedelta(seconds=MAX_CALL_DURATION + 120)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the intercom camera."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomCamera(hub, entry.entry_id)])


def _device_info(entry_id: str) -> DeviceInfo:
    """The one intercom device every entity of this entry belongs to."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


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
        self._attr_device_info = _device_info(entry_id)
        # The panel the stream calls. The dashboard card labels its play
        # button with it and knows that watching it needs no call button.
        # Fixed for the life of the entry: a new plant reloads it.
        default = hub.config.default_panel
        self._attr_extra_state_attributes = {
            ATTR_INTERCOM_ROLE: ROLE_CAMERA,
            ATTR_DEFAULT_PANEL: default.address,
            ATTR_DEFAULT_PANEL_NAME: default.name,
        }

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
        """Never take a still by building a stream.

        Building a stream fetches the AV view, and the AV view places a
        SIP call to the entrance panel. A picture-glance card polling
        the still every ten seconds would ring the door every ten
        seconds and occupy the household's intercom for as long as the
        dashboard stayed open.
        """
        return False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still from the call in progress, or None.

        No path from here can start a call: the picture is decoded from
        the keyframe the video registry already cached for a call that
        is running. Outside a call there is honestly no image to give,
        and `None` is what the camera platform expects to hear.
        """
        if not self._hub.in_call:
            return None
        return await media.snapshot_jpeg()

    async def stream_source(self) -> str | None:
        """Signed URL of the MPEG-TS stream."""
        signed = async_sign_path(self.hass, AV_PATH, SIGNATURE_LIFETIME)
        return f"{get_url(self.hass, prefer_external=False)}{signed}"
