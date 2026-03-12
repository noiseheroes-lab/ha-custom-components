"""Switch entities for Dreame H15 Pro."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    PROP_AUTO_ADD_WATER,
    PROP_AUTO_DRYING,
    PROP_CHILD_LOCK,
    PROP_VOICE_PROMPT,
)
from .coordinator import DreameH15ProCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches from config entry."""
    coordinator: DreameH15ProCoordinator = hass.data[DOMAIN][entry.entry_id]
    did = entry.data[CONF_DEVICE_DID]
    name = entry.data.get(CONF_DEVICE_NAME, "H15 Pro")
    model = entry.data.get(CONF_DEVICE_MODEL, "dreame.hold.w2448e")

    entities = [
        DreameToggleSwitch(
            coordinator, did, name, model,
            key="voice_prompt",
            prop_key=PROP_VOICE_PROMPT,
            siid=1, piid=7,
            entity_name="Avviso vocale",
            icon="mdi:volume-high",
        ),
        DreameToggleSwitch(
            coordinator, did, name, model,
            key="child_lock",
            prop_key=PROP_CHILD_LOCK,
            siid=1, piid=10,
            entity_name="Blocco bambini",
            icon="mdi:lock-outline",
        ),
        DreameToggleSwitch(
            coordinator, did, name, model,
            key="auto_drying",
            prop_key=PROP_AUTO_DRYING,
            siid=1, piid=35,
            entity_name="Asciugatura automatica",
            icon="mdi:fan",
        ),
        DreameToggleSwitch(
            coordinator, did, name, model,
            key="auto_add_water",
            prop_key=PROP_AUTO_ADD_WATER,
            siid=1, piid=73,
            entity_name="Aggiunta acqua automatica",
            icon="mdi:water-plus",
        ),
    ]
    async_add_entities(entities)


class DreameToggleSwitch(
    CoordinatorEntity[DreameH15ProCoordinator], SwitchEntity
):
    """Generic toggle switch for Dreame H15 Pro settings."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DreameH15ProCoordinator,
        did: str,
        device_name: str,
        model: str,
        *,
        key: str,
        prop_key: str,
        siid: int,
        piid: int,
        entity_name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._did = did
        self._prop_key = prop_key
        self._siid = siid
        self._piid = piid
        self._attr_name = entity_name
        self._attr_icon = icon
        self._attr_unique_id = f"{did}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": device_name,
            "manufacturer": "Dreame",
            "model": model,
        }

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._prop_key)
        if val is None:
            return None
        return int(val) == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.api.set_property(
            self._did, self._siid, self._piid, 1
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.api.set_property(
            self._did, self._siid, self._piid, 0
        )
        await self.coordinator.async_request_refresh()
