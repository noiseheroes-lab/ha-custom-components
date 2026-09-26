"""Select platform for Vimar Intercom — the answering machine's delay."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_INTERCOM_ROLE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    ROLE_VOICEMAIL_TIMEOUT,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the voicemail timeout select."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarVoicemailTimeoutSelect(hub, entry.entry_id)])


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarVoicemailTimeoutSelect(SelectEntity):
    """Seconds the unit rings before the answering machine picks up.

    The choices are the ones the indoor unit offers (its
    `vm_timeout_values`), as seconds. A change is sent to the unit and
    only adopted once the unit confirms it; a refusal is raised with the
    unit's own error code.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "voicemail_timeout"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the select to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_voicemail_timeout"
        self._attr_device_info = _device_info(entry_id)
        self._attr_extra_state_attributes = {
            ATTR_INTERCOM_ROLE: ROLE_VOICEMAIL_TIMEOUT}

    @property
    def available(self) -> bool:
        """True once the unit has said which delays it offers."""
        return bool(self._hub.apartment.vm_timeout_values)

    @property
    def options(self) -> list[str]:
        """The delays the unit offers, in seconds."""
        return [str(v) for v in self._hub.apartment.vm_timeout_values]

    @property
    def current_option(self) -> str | None:
        """The delay the unit last reported, if it is one of the choices."""
        current = self._hub.apartment.vm_timeout
        option = None if current is None else str(current)
        return option if option in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Send the new delay and wait for the unit to confirm it."""
        ok, msg = await self._hub.async_set_vm_timeout(int(option))
        if not ok:
            raise HomeAssistantError(msg)

    async def async_added_to_hass(self) -> None:
        """Follow the hub's feature updates."""
        self._hub.register_update_callback(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        """Stop following the hub."""
        self._hub.unregister_update_callback(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()
