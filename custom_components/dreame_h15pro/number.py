"""Number entities for Dreame H15 Pro."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    PROP_DRYING_TEMP,
    PROP_DRYING_TIME,
    PROP_SUCTION_LEVEL,
)
from .coordinator import DreameH15ProCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from config entry."""
    coordinator: DreameH15ProCoordinator = hass.data[DOMAIN][entry.entry_id]
    did = entry.data[CONF_DEVICE_DID]
    name = entry.data.get(CONF_DEVICE_NAME, "H15 Pro")
    model = entry.data.get(CONF_DEVICE_MODEL, "dreame.hold.w2448e")

    entities = [
        DreameSuctionNumberEntity(
            coordinator, did, name, model,
        ),
        DreameNumberEntity(
            coordinator, did, name, model,
            key="drying_temp",
            prop_key=PROP_DRYING_TEMP,
            siid=1, piid=34,
            entity_name="Temperatura asciugatura",
            icon="mdi:thermometer",
            native_min=25,
            native_max=55,
            native_step=1,
            unit=UnitOfTemperature.CELSIUS,
        ),
        DreameNumberEntity(
            coordinator, did, name, model,
            key="drying_time",
            prop_key=PROP_DRYING_TIME,
            siid=1, piid=50,
            entity_name="Durata asciugatura",
            icon="mdi:timer-outline",
            native_min=5,
            native_max=120,
            native_step=5,
            unit=UnitOfTime.MINUTES,
        ),
    ]
    async_add_entities(entities)


class DreameNumberEntity(
    CoordinatorEntity[DreameH15ProCoordinator], NumberEntity
):
    """Numeric control for Dreame H15 Pro."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER

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
        native_min: float,
        native_max: float,
        native_step: float,
        unit: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._did = did
        self._prop_key = prop_key
        self._siid = siid
        self._piid = piid
        self._attr_name = entity_name
        self._attr_icon = icon
        self._attr_unique_id = f"{did}_{key}"
        self._attr_native_min_value = native_min
        self._attr_native_max_value = native_max
        self._attr_native_step = native_step
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": device_name,
            "manufacturer": "Dreame",
            "model": model,
        }

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self._prop_key)
        if val is None:
            return None
        return float(val)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.api.set_property(
            self._did, self._siid, self._piid, int(value)
        )
        await self.coordinator.async_request_refresh()


class DreameSuctionNumberEntity(
    CoordinatorEntity[DreameH15ProCoordinator], NumberEntity
):
    """Suction level control (4.5 — array value)."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_name = "Livello aspirazione"
    _attr_icon = "mdi:fan"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, did, device_name, model) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"{did}_suction_level"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": device_name,
            "manufacturer": "Dreame",
            "model": model,
        }

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data.get(PROP_SUCTION_LEVEL)
        if raw is None:
            return None
        # Parse array like "[81]"
        import json
        try:
            arr = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(arr, list) and arr:
                return float(arr[0])
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.api.set_property(
            self._did, 4, 5, [int(value)]
        )
        await self.coordinator.async_request_refresh()
