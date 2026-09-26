"""Lock platform for Vimar Intercom."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_INTERCOM_ROLE, DOMAIN, MANUFACTURER, MODEL, ROLE_DOOR
from .entity_plan import LockPlan

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the door locks the entity plan lists.

    Without a phonebook that is one lock, addressing the relay group
    from the QR: the door a Vimar system has by default, working without
    the user configuring anything. With one, every door actuator the
    installer set up is a lock under its own name, and the one that is
    the same door as the old lock keeps its unique ID (see
    `entity_plan`).
    """
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        VimarIntercomLock(data["hub"], entry.entry_id, lock)
        for lock in data["plan"].locks)


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarIntercomLock(LockEntity):
    """A door release, modelled as a lock.

    Unlocking sends the SIP door command to the panel, which pulses its
    relay. The physical release re-locks itself after a few seconds, so
    the entity returns to locked after the same delay.

    The generic lock (no phonebook, or no phonebook actuator that is the
    same door) leaves target and command to the hub, which picks
    OPEN_CURRENT during a call. A phonebook lock always sends its own
    command to its own target: it is named after one door, and must not
    open another one because somebody else happens to be calling.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "door"
    _attr_icon = "mdi:door-closed-lock"
    # Every lock is a door release; the card shows it as an open button.
    _attr_extra_state_attributes = {ATTR_INTERCOM_ROLE: ROLE_DOOR}

    def __init__(self, hub, entry_id: str, plan: LockPlan) -> None:
        self._hub = hub
        self._plan = plan
        self._attr_unique_id = f"{entry_id}_{plan.unique_suffix}"
        if plan.name is not None:
            self._attr_name = plan.name
        self._is_locked = True
        self._relock_task: asyncio.Task | None = None
        self._attr_device_info = _device_info(entry_id)

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
        """Open the door via SIP MESSAGE.

        A failure is raised, not just logged. The hub has already
        produced the sentence the user needs — why it failed and what to
        do — and swallowing it left them pressing Open door, seeing
        nothing happen, and having no way to tell a slow door from a
        dead connection. Raising also lets an automation catch it.
        """
        ok, msg = await self._hub.async_door(
            target=self._plan.target, command=self._plan.command)
        if not ok:
            _LOGGER.error("Door open failed: %s", msg)
            raise HomeAssistantError(msg)

        self._is_locked = False
        self.async_write_ha_state()
        if self._relock_task:
            self._relock_task.cancel()
        self._relock_task = asyncio.create_task(self._auto_relock())

    async def _auto_relock(self):
        await asyncio.sleep(5)
        self._is_locked = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the re-lock timer before the entity goes.

        An entry reload within the five seconds after the door opened
        otherwise wakes this task on a removed entity, and
        `async_write_ha_state` on one of those raises.
        """
        if self._relock_task:
            self._relock_task.cancel()
            self._relock_task = None
