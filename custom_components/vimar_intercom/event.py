"""Event platform for Vimar Intercom — doorbell ring detection."""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    async_add_entities([VimarDoorbellEvent(hub, entry.entry_id)])


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarDoorbellEvent(EventEntity):
    """Fires when an entrance panel calls this Home Assistant.

    The same ring is also published on the event bus as
    `vimar_intercom_ring`, which is the supported integration point for
    notifications and companion apps.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell"
    _attr_icon = "mdi:bell-ring"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_doorbell"
        self._attr_device_info = _device_info(entry_id)

    async def async_added_to_hass(self) -> None:
        """Register ring callback when entity is added."""
        self._hub.register_ring_callback(self._handle_ring)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister ring callback when entity is removed."""
        self._hub.unregister_ring_callback(self._handle_ring)

    @callback
    def _handle_ring(self, panel: str) -> None:
        """Record the ring, tagged with the panel that called."""
        self._trigger_event("ring", {"panel": panel})
        self.async_write_ha_state()
        _LOGGER.info("Doorbell ring from panel %s", panel)
