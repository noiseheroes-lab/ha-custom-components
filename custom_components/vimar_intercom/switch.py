"""Switch platform for Vimar Intercom — do-not-disturb and voicemail.

Both are apartment-wide settings kept on the indoor unit, not on this
Home Assistant: turning one on here turns it on for every device of the
apartment, exactly as it does from the official app, and a change made
on the indoor unit or from a phone shows up here.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_INTERCOM_ROLE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    ROLE_DND,
    ROLE_VOICEMAIL,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the do-not-disturb and voicemail switches."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        VimarDndSwitch(hub, entry.entry_id),
        VimarVoicemailSwitch(hub, entry.entry_id),
    ])


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarApartmentSwitch(SwitchEntity):
    """An on/off setting of the apartment, as the indoor unit reports it.

    Unavailable until the unit has reported it, and when the phonebook
    names no apartment intercom address to send a change to: a switch
    that shows a guess, or cannot act, is worse than none.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    # The ApartmentState field it shows, the hub method that changes it,
    # and its role for the dashboard card.
    _field: str
    _setter: str
    _role: str

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the switch to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{self._field}"
        self._attr_device_info = _device_info(entry_id)
        self._attr_extra_state_attributes = {ATTR_INTERCOM_ROLE: self._role}

    @property
    def available(self) -> bool:
        """True when the state is known and a change can be sent."""
        return (self._hub.apartment_intercom is not None
                and getattr(self._hub.apartment, self._field) is not None)

    @property
    def is_on(self) -> bool | None:
        """The state the indoor unit last reported."""
        return getattr(self._hub.apartment, self._field)

    async def _async_set(self, on: bool) -> None:
        """Send the change; raising puts the reason in front of the user."""
        ok, msg = await getattr(self._hub, self._setter)(on)
        if not ok:
            raise HomeAssistantError(msg)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the setting on for the whole apartment."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the setting off for the whole apartment."""
        await self._async_set(False)

    async def async_added_to_hass(self) -> None:
        """Follow the hub's feature updates."""
        self._hub.register_update_callback(self._on_update)

    async def async_will_remove_from_hass(self) -> None:
        """Stop following the hub."""
        self._hub.unregister_update_callback(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class VimarDndSwitch(VimarApartmentSwitch):
    """Do not disturb: the indoor unit and the apps stop ringing."""

    _attr_translation_key = "dnd"
    _attr_icon = "mdi:bell-off"
    _field = "dnd"
    _setter = "async_set_dnd"
    _role = ROLE_DND


class VimarVoicemailSwitch(VimarApartmentSwitch):
    """The answering machine: an unanswered visitor can leave a message."""

    _attr_translation_key = "voicemail"
    _attr_icon = "mdi:voicemail"
    _field = "voicemail"
    _setter = "async_set_voicemail"
    _role = ROLE_VOICEMAIL
