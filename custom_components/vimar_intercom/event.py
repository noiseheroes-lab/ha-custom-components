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


class VimarDoorbellEvent(EventEntity):
    """Doorbell ring event — fires when an incoming SIP INVITE is received.

    In HomeKit this maps to a Doorbell accessory, enabling push
    notifications with video snapshot on Apple devices.
    """

    _attr_has_entity_name = False
    _attr_name = "Doorbell"
    _attr_icon = "mdi:bell-ring"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_doorbell"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_added_to_hass(self) -> None:
        """Register ring callback when entity is added."""
        self._hub.register_ring_callback(self._handle_ring)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister ring callback when entity is removed."""
        self._hub.unregister_ring_callback(self._handle_ring)

    @callback
    def _handle_ring(self) -> None:
        """Handle incoming SIP INVITE (doorbell ring)."""
        self._trigger_event("ring")
        self.async_write_ha_state()
        _LOGGER.info("Doorbell ring detected")
