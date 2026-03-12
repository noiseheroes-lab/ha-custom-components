"""Lock platform for Vimar Intercom."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lock import LockEntity
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
        VimarIntercomLock(hub, entry.entry_id, key="lock", name="Street Gate", door_target="55001", door_command="OPEN_2F"),
        VimarIntercomLock(hub, entry.entry_id, key="lock_2", name="Building Door", door_target="55002", door_command="OPEN_2F"),
    ])


class VimarIntercomLock(LockEntity):
    """Door lock — unlock sends SIP MESSAGE to open the intercom relay.

    In HomeKit this maps to a DoorLock accessory. The physical lock
    auto-relocks after a few seconds, so we transition back to locked
    after 5 seconds.
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:door-closed-lock"

    def __init__(self, hub, entry_id: str, *, key: str, name: str, door_target: str | None, door_command: str = "OPEN_2F") -> None:
        self._hub = hub
        self._door_target = door_target
        self._door_command = door_command
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}_{key}"
        self._is_locked = True
        self._relock_task: asyncio.Task | None = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    @property
    def icon(self) -> str:
        return "mdi:door-closed-lock" if self._is_locked else "mdi:door-open"

    async def async_lock(self, **kwargs) -> None:
        """No-op: door auto-relocks."""
        self._is_locked = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        """Open the door via SIP MESSAGE."""
        ok, msg = await self._hub.async_door(target=self._door_target, command=self._door_command)
        if ok:
            self._is_locked = False
            self.async_write_ha_state()
            if self._relock_task:
                self._relock_task.cancel()
            self._relock_task = asyncio.create_task(self._auto_relock())
        else:
            _LOGGER.error("Door open failed: %s", msg)

    async def _auto_relock(self):
        await asyncio.sleep(5)
        self._is_locked = True
        self.async_write_ha_state()
