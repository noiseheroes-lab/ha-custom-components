"""Button platform for Vimar Intercom."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one call and one door button per configured panel."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    entities: list[ButtonEntity] = [
        VimarAnswerButton(hub, entry.entry_id),
        VimarHangupButton(hub, entry.entry_id),
        VimarReconnectButton(hub, entry.entry_id),
    ]
    for panel in hub.config.panels:
        entities.append(VimarCallButton(hub, entry.entry_id, panel))
        entities.append(VimarDoorButton(hub, entry.entry_id, panel))
    async_add_entities(entities)


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarButtonBase(ButtonEntity):
    """Common wiring for the intercom buttons."""

    _attr_has_entity_name = True

    def __init__(self, hub, entry_id: str, unique_suffix: str) -> None:
        """Attach the button to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_device_info = _device_info(entry_id)


class VimarCallButton(VimarButtonBase):
    """Call one entrance panel."""

    _attr_translation_key = "call"
    _attr_icon = "mdi:phone-outgoing"

    def __init__(self, hub, entry_id: str, panel) -> None:
        """Remember which panel this button calls."""
        super().__init__(hub, entry_id, f"call_{panel.address}")
        self._panel = panel
        self._attr_name = f"Call {panel.name}"

    async def async_press(self) -> None:
        """Place the call.

        Raising is what puts the reason in front of the user: a button
        press that only logs looks identical to one that worked.
        """
        ok, msg = await self._hub.async_call(target=self._panel.address)
        if not ok:
            _LOGGER.error("Call to %s failed: %s", self._panel.address, msg)
            raise HomeAssistantError(msg)


class VimarDoorButton(VimarButtonBase):
    """Open the door of one entrance panel."""

    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"

    def __init__(self, hub, entry_id: str, panel) -> None:
        """Remember which panel this button opens."""
        super().__init__(hub, entry_id, f"door_{panel.address}")
        self._panel = panel
        self._attr_name = f"Open {panel.name}"

    async def async_press(self) -> None:
        """Send the door command."""
        ok, msg = await self._hub.async_door(target=self._panel.address)
        if not ok:
            _LOGGER.error("Opening %s failed: %s", self._panel.address, msg)
            raise HomeAssistantError(msg)


class VimarAnswerButton(VimarButtonBase):
    """Answer an incoming intercom call."""

    _attr_translation_key = "answer"
    _attr_icon = "mdi:phone-incoming"

    def __init__(self, hub, entry_id: str) -> None:
        """Create the answer button."""
        super().__init__(hub, entry_id, "answer")

    async def async_press(self) -> None:
        """Answer the pending call."""
        ok, msg = await self._hub.async_answer()
        if not ok:
            _LOGGER.error("Answer failed: %s", msg)
            raise HomeAssistantError(msg)


class VimarHangupButton(VimarButtonBase):
    """End the current call."""

    _attr_translation_key = "hang_up"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, hub, entry_id: str) -> None:
        """Create the hang-up button."""
        super().__init__(hub, entry_id, "hangup")

    async def async_press(self) -> None:
        """Send BYE."""
        await self._hub.async_hangup()


class VimarReconnectButton(VimarButtonBase):
    """Rebuild the SIP connection without restarting Home Assistant."""

    _attr_translation_key = "reconnect"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, entry_id: str) -> None:
        """Create the reconnect button."""
        super().__init__(hub, entry_id, "reconnect")

    async def async_press(self) -> None:
        """Drop the connection so the supervisor rebuilds it."""
        await self._hub.async_reconnect()
