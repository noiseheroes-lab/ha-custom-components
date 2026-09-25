"""Binary sensor platform for Vimar Intercom."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SIP registration and in-call binary sensors."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        VimarSIPRegistrationSensor(hub, entry.entry_id),
        VimarInCallSensor(hub, entry.entry_id),
    ])


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarSIPRegistrationSensor(BinarySensorEntity):
    """Shows whether SIP registration is active."""

    _attr_has_entity_name = True
    _attr_translation_key = "sip_registration"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:lan-connect"

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the sensor to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_sip_registered"
        self._attr_device_info = _device_info(entry_id)

    @property
    def is_on(self) -> bool:
        """True while the SIP client holds a valid registration."""
        return self._hub.registered

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the proxy the client is talking to."""
        cfg = self._hub.config
        return {
            "proxy_host": cfg.proxy_host,
            "proxy_port": str(cfg.proxy_port),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to hub state changes."""
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from hub state changes."""
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()


class VimarInCallSensor(BinarySensorEntity):
    """Shows whether there is an active SIP call."""

    _attr_has_entity_name = True
    _attr_translation_key = "in_call"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the sensor to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_in_call"
        self._attr_device_info = _device_info(entry_id)

    @property
    def is_on(self) -> bool:
        """True while a SIP call is active."""
        return self._hub.in_call

    async def async_added_to_hass(self) -> None:
        """Subscribe to hub state changes."""
        self._hub.register_state_callback(self._on_state_change)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from hub state changes."""
        self._hub.unregister_state_callback(self._on_state_change)

    @callback
    def _on_state_change(self) -> None:
        self.async_write_ha_state()
