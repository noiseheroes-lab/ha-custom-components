"""Binary sensor entities for Dreame H15 Pro."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CLEANING_STATUSES,
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    PROP_WATER_TANK,
)
from .coordinator import DreameH15ProCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors from config entry."""
    coordinator: DreameH15ProCoordinator = hass.data[DOMAIN][entry.entry_id]
    did = entry.data[CONF_DEVICE_DID]
    name = entry.data.get(CONF_DEVICE_NAME, "H15 Pro")
    model = entry.data.get(CONF_DEVICE_MODEL, "dreame.hold.w2448e")

    entities = [
        DreameWaterTankSensor(coordinator, did, name, model),
        DreameCleaningSensor(coordinator, did, name, model),
    ]
    async_add_entities(entities)


class DreameBaseBinarySensor(
    CoordinatorEntity[DreameH15ProCoordinator], BinarySensorEntity
):
    """Base binary sensor for Dreame H15 Pro."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DreameH15ProCoordinator,
        did: str,
        name: str,
        model: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._did = did
        self._key = key
        self._attr_unique_id = f"{did}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": name,
            "manufacturer": "Dreame",
            "model": model,
        }


class DreameWaterTankSensor(DreameBaseBinarySensor):
    """Water tank present binary sensor."""

    _attr_name = "Serbatoio acqua"
    _attr_icon = "mdi:water"

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "water_tank")

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_WATER_TANK)
        return val is not None and int(val) != 0


class DreameCleaningSensor(DreameBaseBinarySensor):
    """Cleaning in progress binary sensor."""

    _attr_name = "In funzione"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "cleaning")

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        status = self.coordinator.data.get("status", "")
        return status in CLEANING_STATUSES
