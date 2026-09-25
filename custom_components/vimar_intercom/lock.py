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
    """Create the door lock.

    One entity, addressing the relay group from the QR. That is the door
    a Vimar system has by default, and it works without the user
    configuring anything. Plants with a second entrance reach it through
    the per-panel door buttons.
    """
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomLock(hub, entry.entry_id)])


class VimarIntercomLock(LockEntity):
    """A door release, modelled as a lock.

    Unlocking sends the SIP door command to the panel, which pulses its
    relay. The physical release re-locks itself after a few seconds, so
    the entity returns to locked after the same delay.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "door"
    _attr_icon = "mdi:door-closed-lock"

    def __init__(self, hub, entry_id: str) -> None:
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_lock"
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
        ok, msg = await self._hub.async_door()
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
