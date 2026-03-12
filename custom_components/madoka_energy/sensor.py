"""Sensor platform for Daikin Madoka Energy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ble_client import MadokaData
from .const import CONF_DEVICE_ADDRESS, DOMAIN
from .coordinator import MadokaCoordinator


@dataclass(frozen=True, kw_only=True)
class MadokaSensorDescription(SensorEntityDescription):
    """Sensor description with value extractor."""

    value_fn: Callable[[MadokaData], float | None]


SENSORS: list[MadokaSensorDescription] = [
    # Daily energy
    MadokaSensorDescription(
        key="energy_today_total",
        translation_key="energy_today_total",
        name="Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.day_energy.current.total,
    ),
    MadokaSensorDescription(
        key="energy_yesterday_total",
        translation_key="energy_yesterday_total",
        name="Energy Yesterday",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.day_energy.previous.total,
    ),
    # Weekly energy
    MadokaSensorDescription(
        key="energy_this_week_total",
        translation_key="energy_this_week_total",
        name="Energy This Week",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.week_energy.current.total,
    ),
    MadokaSensorDescription(
        key="energy_last_week_total",
        translation_key="energy_last_week_total",
        name="Energy Last Week",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.week_energy.previous.total,
    ),
    # Yearly energy
    MadokaSensorDescription(
        key="energy_this_year_total",
        translation_key="energy_this_year_total",
        name="Energy This Year",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.year_energy.current.total,
    ),
    MadokaSensorDescription(
        key="energy_last_year_total",
        translation_key="energy_last_year_total",
        name="Energy Last Year",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.year_energy.previous.total,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Madoka Energy sensors."""
    coordinator: MadokaCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_DEVICE_ADDRESS]

    async_add_entities(
        MadokaEnergySensor(coordinator, description, address)
        for description in SENSORS
    )


class MadokaEnergySensor(CoordinatorEntity[MadokaCoordinator], SensorEntity):
    """Sensor for Madoka energy data."""

    entity_description: MadokaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MadokaCoordinator,
        description: MadokaSensorDescription,
        address: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._address = address
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            name=f"Madoka {address[-8:]}",
            manufacturer="Daikin",
            model="BRC1H",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return extra attributes with consumption breakdown."""
        if self.coordinator.data is None:
            return None

        key = self.entity_description.key
        data = self.coordinator.data

        if key == "energy_today_total" and data.day_energy.current.consumption:
            slots = [
                "00-02", "02-04", "04-06", "06-08", "08-10", "10-12",
                "12-14", "14-16", "16-18", "18-20", "20-22", "22-24",
            ]
            return {
                slots[i] if i < len(slots) else f"slot_{i}": v
                for i, v in enumerate(data.day_energy.current.consumption)
                if v is not None
            }

        if key == "energy_this_week_total" and data.week_energy.current.consumption:
            days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            return {
                days[i] if i < len(days) else f"day_{i}": v
                for i, v in enumerate(data.week_energy.current.consumption)
                if v is not None
            }

        if key == "energy_this_year_total" and data.year_energy.current.consumption:
            months = [
                "jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec",
            ]
            return {
                months[i] if i < len(months) else f"month_{i}": v
                for i, v in enumerate(data.year_energy.current.consumption)
                if v is not None
            }

        return None
