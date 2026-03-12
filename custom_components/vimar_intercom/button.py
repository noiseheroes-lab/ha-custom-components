"""Button platform for Vimar Intercom."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    async_add_entities([
        VimarCallButton(hub, entry.entry_id),
        VimarCallTargetButton(hub, entry.entry_id, "55001", "Chiama Targa Esterna", "call_ext"),
        VimarCallTargetButton(hub, entry.entry_id, "60001", "Chiama Targa Interna", "call_int"),
        VimarAnswerButton(hub, entry.entry_id),
        VimarHangupButton(hub, entry.entry_id),
        VimarDoorButton(hub, entry.entry_id, "55001", "Apri Cancello", "door_street", "mdi:gate"),
        VimarDoorButton(hub, entry.entry_id, "55002", "Apri Portone", "door_building", "mdi:door"),
    ])


class VimarCallButton(ButtonEntity):
    """Button to call the intercom (initiate SIP INVITE)."""

    _attr_has_entity_name = False
    _attr_name = "Chiama"
    _attr_icon = "mdi:phone-outgoing"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_call"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_call()
        if not ok:
            _LOGGER.error("Call failed: %s", msg)


class VimarCallTargetButton(ButtonEntity):
    """Button to call a specific SIP target (targa interna/esterna)."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:phone-outgoing"

    def __init__(self, hub, entry_id: str, target: str, name: str, key: str) -> None:
        self._hub = hub
        self._target = target
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_call(target=self._target)
        if not ok:
            _LOGGER.error("Call to %s failed: %s", self._target, msg)


class VimarAnswerButton(ButtonEntity):
    """Button to answer an incoming intercom call."""

    _attr_has_entity_name = False
    _attr_name = "Rispondi"
    _attr_icon = "mdi:phone-incoming"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_answer"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_answer()
        if not ok:
            _LOGGER.error("Answer failed: %s", msg)


class VimarHangupButton(ButtonEntity):
    """Button to hang up the current call (SIP BYE)."""

    _attr_has_entity_name = False
    _attr_name = "Riaggancia"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_hangup"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_press(self) -> None:
        await self._hub.async_hangup()


class VimarDoorButton(ButtonEntity):
    """Button to open a door (SIP MESSAGE)."""

    _attr_has_entity_name = False

    def __init__(self, hub, entry_id: str, target: str | None, name: str, key: str, icon: str) -> None:
        self._hub = hub
        self._target = target
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_press(self) -> None:
        ok, msg = await self._hub.async_door(target=self._target)
        if not ok:
            _LOGGER.error("Door %s open failed: %s", self._target or "default", msg)
