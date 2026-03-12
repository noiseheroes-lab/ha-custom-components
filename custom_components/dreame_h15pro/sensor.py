"""Sensor entities for Dreame H15 Pro."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    ERROR_MAP,
    PROP_BATTERY,
    PROP_CLEAN_AREA,
    PROP_CLEAN_TIME,
    PROP_CONSUMABLE_1_68,
    PROP_CONSUMABLE_1_69,
    PROP_CONSUMABLE_1_70,
    PROP_CONSUMABLE_1_71,
    PROP_ERROR_CODE,
    PROP_FILTER_LIFE,
    PROP_HEPA_LIFE,
    PROP_LAST_ACTIVITY,
    PROP_ROLLER_LIFE,
    PROP_RUNTIME_SECONDARY,
    PROP_SENSOR_DIRTY_LEVEL,
    PROP_SENSOR_DIRTY_TIME,
    PROP_TOTAL_CLEANS,
    PROP_TOTAL_RUNTIME,
    PROP_TOTAL_SELF_CLEANS,
    PROP_WARN_CODE,
    PROP_WATER_TEMP,
    PROP_WORK_MODE,
    STATUS_DISPLAY,
    WARN_MAP,
)
from .coordinator import DreameH15ProCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from config entry."""
    coordinator: DreameH15ProCoordinator = hass.data[DOMAIN][entry.entry_id]
    did = entry.data[CONF_DEVICE_DID]
    name = entry.data.get(CONF_DEVICE_NAME, "H15 Pro")
    model = entry.data.get(CONF_DEVICE_MODEL, "dreame.hold.w2448e")

    entities = [
        # Original sensors
        DreameStatusSensor(coordinator, did, name, model),
        DreameBatterySensor(coordinator, did, name, model),
        DreameCleanTimeSensor(coordinator, did, name, model),
        DreameCleanAreaSensor(coordinator, did, name, model),
        DreameTotalRuntimeSensor(coordinator, did, name, model),
        # New sensors
        DreameWaterTempSensor(coordinator, did, name, model),
        DreameTotalCleansSensor(coordinator, did, name, model),
        DreameTotalSelfCleansSensor(coordinator, did, name, model),
        DreameFilterLifeSensor(coordinator, did, name, model),
        DreameRollerLifeSensor(coordinator, did, name, model),
        DreameHepaLifeSensor(coordinator, did, name, model),
        DreameLastActivitySensor(coordinator, did, name, model),
        DreameErrorSensor(coordinator, did, name, model),
        DreameWarnSensor(coordinator, did, name, model),
        DreameSensorDirtySensor(coordinator, did, name, model),
        DreameRuntimeSecondarySensor(coordinator, did, name, model),
    ]
    async_add_entities(entities)


class DreameBaseSensor(CoordinatorEntity[DreameH15ProCoordinator], SensorEntity):
    """Base sensor for Dreame H15 Pro."""

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


# ── Original sensors ─────────────────────────────────────────────────


class DreameStatusSensor(DreameBaseSensor):
    """Device status sensor."""

    _attr_name = "Stato"
    _attr_icon = "mdi:robot-vacuum"

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "status")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        status = self.coordinator.data.get("status", "unknown")
        return STATUS_DISPLAY.get(status, status)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "status_code": self.coordinator.data.get("status_code"),
            "status_key": self.coordinator.data.get("status"),
        }


class DreameBatterySensor(DreameBaseSensor):
    """Battery level sensor."""

    _attr_name = "Batteria"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "battery")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_BATTERY)
        return int(val) if val is not None else None


class DreameCleanTimeSensor(DreameBaseSensor):
    """Clean time sensor (minutes)."""

    _attr_name = "Tempo pulizia"
    _attr_icon = "mdi:timer-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "clean_time")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_CLEAN_TIME)
        return int(val) if val is not None else None


class DreameCleanAreaSensor(DreameBaseSensor):
    """Clean area sensor (m²)."""

    _attr_name = "Area pulita"
    _attr_icon = "mdi:texture-box"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "m²"

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "clean_area")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_CLEAN_AREA)
        if val is not None:
            return round(float(val), 1)
        return None


class DreameTotalRuntimeSensor(DreameBaseSensor):
    """Total runtime sensor (hours)."""

    _attr_name = "Tempo utilizzo totale"
    _attr_icon = "mdi:clock-outline"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "total_runtime")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_TOTAL_RUNTIME)
        if val is not None:
            return round(int(val) / 60, 1)  # minutes to hours
        return None


# ── New sensors ──────────────────────────────────────────────────────


class DreameWaterTempSensor(DreameBaseSensor):
    """Water temperature sensor."""

    _attr_name = "Temperatura acqua"
    _attr_icon = "mdi:thermometer-water"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "water_temp")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_WATER_TEMP)
        return int(val) if val is not None else None


class DreameTotalCleansSensor(DreameBaseSensor):
    """Total cleaning sessions counter."""

    _attr_name = "Pulizie totali"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "total_cleans")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_TOTAL_CLEANS)
        return int(val) if val is not None else None


class DreameTotalSelfCleansSensor(DreameBaseSensor):
    """Total self-cleaning sessions counter."""

    _attr_name = "Autopulizie totali"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "total_self_cleans")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_TOTAL_SELF_CLEANS)
        return int(val) if val is not None else None


class DreameFilterLifeSensor(DreameBaseSensor):
    """Filter life remaining."""

    _attr_name = "Vita filtro"
    _attr_icon = "mdi:air-filter"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "filter_life")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_FILTER_LIFE)
        return int(val) if val is not None else None


class DreameRollerLifeSensor(DreameBaseSensor):
    """Roller brush life remaining."""

    _attr_name = "Vita rullo"
    _attr_icon = "mdi:rotate-3d-variant"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "roller_life")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_ROLLER_LIFE)
        return int(val) if val is not None else None


class DreameHepaLifeSensor(DreameBaseSensor):
    """HEPA filter life remaining."""

    _attr_name = "Vita HEPA"
    _attr_icon = "mdi:air-filter"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "hepa_life")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_HEPA_LIFE)
        return int(val) if val is not None else None


class DreameLastActivitySensor(DreameBaseSensor):
    """Last activity timestamp sensor."""

    _attr_name = "Ultima attivita"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "last_activity")

    @property
    def native_value(self) -> datetime | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_LAST_ACTIVITY)
        if val is None:
            return None
        try:
            ts = int(val)
            if ts > 0:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            pass
        return None


class DreameErrorSensor(DreameBaseSensor):
    """Error code sensor."""

    _attr_name = "Errore"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "error")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_ERROR_CODE)
        if val is None:
            return None
        code = int(val)
        desc = ERROR_MAP.get(code)
        if desc is None and code == 0:
            return "Nessuno"
        return desc or f"Errore {code}"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {"error_code": self.coordinator.data.get(PROP_ERROR_CODE)}


class DreameWarnSensor(DreameBaseSensor):
    """Warning code sensor."""

    _attr_name = "Avviso"
    _attr_icon = "mdi:alert-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "warning")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_WARN_CODE)
        if val is None:
            return None
        code = int(val)
        desc = WARN_MAP.get(code)
        if desc is None and code == 0:
            return "Nessuno"
        return desc or f"Avviso {code}"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {"warn_code": self.coordinator.data.get(PROP_WARN_CODE)}


class DreameSensorDirtySensor(DreameBaseSensor):
    """Sensor dirty level."""

    _attr_name = "Livello sporco sensore"
    _attr_icon = "mdi:spray-bottle"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "sensor_dirty")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_SENSOR_DIRTY_LEVEL)
        return int(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {
            "dirty_time_left": self.coordinator.data.get(PROP_SENSOR_DIRTY_TIME),
        }


class DreameRuntimeSecondarySensor(DreameBaseSensor):
    """Secondary runtime counter (hours)."""

    _attr_name = "Runtime secondario"
    _attr_icon = "mdi:clock-outline"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, did, name, model):
        super().__init__(coordinator, did, name, model, "runtime_secondary")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(PROP_RUNTIME_SECONDARY)
        if val is not None:
            return round(int(val) / 60, 1)  # minutes to hours
        return None
