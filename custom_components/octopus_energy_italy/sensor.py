"""Sensor platform for Octopus Energy Italy."""

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
from homeassistant.const import (
    CURRENCY_EURO,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import OctopusData
from .const import (
    CONF_ACCOUNT_NUMBER,
    DATA_COORDINATOR,
    DOMAIN,
    MANUFACTURER,
    SENSOR_ACCOUNT_BALANCE,
    SENSOR_ELECTRICITY_MONTHLY,
    SENSOR_ELECTRICITY_RATE,
    SENSOR_ELECTRICITY_STANDING,
    SENSOR_ELECTRICITY_YEARLY,
    SENSOR_ELECTRICITY_YESTERDAY,
    SENSOR_GAS_MONTHLY,
    SENSOR_GAS_RATE,
    SENSOR_GAS_STANDING,
    SENSOR_GAS_YEARLY,
)
from .coordinator import OctopusEnergyCoordinator

# Unit for gas (Smc ≈ Standard cubic metres — HA has no specific unit)
UNIT_SMC = "Smc"
UNIT_EUR_KWH = "€/kWh"
UNIT_EUR_SMC = "€/Smc"
UNIT_EUR_YEAR = "€/year"


@dataclass(frozen=True, kw_only=True)
class OctopusSensorDescription(SensorEntityDescription):
    """Description with a value extractor function."""
    value_fn: Callable[[OctopusData], float | str | None]


ELECTRICITY_SENSORS: tuple[OctopusSensorDescription, ...] = (
    OctopusSensorDescription(
        key=SENSOR_ELECTRICITY_YESTERDAY,
        translation_key=SENSOR_ELECTRICITY_YESTERDAY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:flash-outline",
        value_fn=lambda d: d.electricity_yesterday_kwh,
    ),
    OctopusSensorDescription(
        key=SENSOR_ELECTRICITY_MONTHLY,
        translation_key=SENSOR_ELECTRICITY_MONTHLY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        icon="mdi:flash",
        value_fn=lambda d: d.electricity_monthly_kwh,
    ),
    OctopusSensorDescription(
        key=SENSOR_ELECTRICITY_YEARLY,
        translation_key=SENSOR_ELECTRICITY_YEARLY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:flash",
        value_fn=lambda d: d.electricity_yearly_kwh,
    ),
    OctopusSensorDescription(
        key=SENSOR_ELECTRICITY_RATE,
        translation_key=SENSOR_ELECTRICITY_RATE,
        native_unit_of_measurement=UNIT_EUR_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        icon="mdi:currency-eur",
        value_fn=lambda d: d.electricity_rate,
    ),
    OctopusSensorDescription(
        key=SENSOR_ELECTRICITY_STANDING,
        translation_key=SENSOR_ELECTRICITY_STANDING,
        native_unit_of_measurement=UNIT_EUR_YEAR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:currency-eur",
        value_fn=lambda d: d.electricity_standing_year,
    ),
)

GAS_SENSORS: tuple[OctopusSensorDescription, ...] = (
    OctopusSensorDescription(
        key=SENSOR_GAS_MONTHLY,
        translation_key=SENSOR_GAS_MONTHLY,
        native_unit_of_measurement=UNIT_SMC,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        icon="mdi:fire",
        value_fn=lambda d: d.gas_monthly_smc,
    ),
    OctopusSensorDescription(
        key=SENSOR_GAS_YEARLY,
        translation_key=SENSOR_GAS_YEARLY,
        native_unit_of_measurement=UNIT_SMC,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:fire",
        value_fn=lambda d: d.gas_yearly_smc,
    ),
    OctopusSensorDescription(
        key=SENSOR_GAS_RATE,
        translation_key=SENSOR_GAS_RATE,
        native_unit_of_measurement=UNIT_EUR_SMC,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        icon="mdi:currency-eur",
        value_fn=lambda d: d.gas_rate,
    ),
    OctopusSensorDescription(
        key=SENSOR_GAS_STANDING,
        translation_key=SENSOR_GAS_STANDING,
        native_unit_of_measurement=UNIT_EUR_YEAR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:currency-eur",
        value_fn=lambda d: d.gas_standing_year,
    ),
)

ACCOUNT_SENSORS: tuple[OctopusSensorDescription, ...] = (
    OctopusSensorDescription(
        key=SENSOR_ACCOUNT_BALANCE,
        translation_key=SENSOR_ACCOUNT_BALANCE,
        native_unit_of_measurement=CURRENCY_EURO,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:cash-multiple",
        value_fn=lambda d: d.account_balance,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Octopus Energy Italy sensors from config entry."""
    coordinator: OctopusEnergyCoordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]
    account_number: str = entry.data[CONF_ACCOUNT_NUMBER]

    entities: list[OctopusEnergySensor] = []

    for description in (*ELECTRICITY_SENSORS, *GAS_SENSORS, *ACCOUNT_SENSORS):
        entities.append(OctopusEnergySensor(coordinator, description, account_number))

    async_add_entities(entities)


class OctopusEnergySensor(CoordinatorEntity[OctopusEnergyCoordinator], SensorEntity):
    """A single Octopus Energy Italy sensor."""

    entity_description: OctopusSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OctopusEnergyCoordinator,
        description: OctopusSensorDescription,
        account_number: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._account_number = account_number
        self._attr_unique_id = f"{account_number}_{description.key}"
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, account_number)},
            manufacturer=MANUFACTURER,
            name=f"Octopus Energy {account_number}",
            configuration_url="https://octopusenergy.it/area-personale",
        )

    @property
    def native_value(self) -> float | str | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return True if coordinator has data and value is not None."""
        return (
            super().available
            and self.coordinator.data is not None
            and self.entity_description.value_fn(self.coordinator.data) is not None
        )

    @property
    def extra_state_attributes(self) -> dict | None:
        """Return extra attributes."""
        if not self.coordinator.data:
            return None
        data = self.coordinator.data
        key = self.entity_description.key

        if key in (SENSOR_ELECTRICITY_YESTERDAY, SENSOR_ELECTRICITY_MONTHLY, SENSOR_ELECTRICITY_YEARLY):
            return {
                "pod": data.electricity_pod,
                "rate_eur_kwh": data.electricity_rate,
                "account_number": data.account_number,
            }
        if key in (SENSOR_GAS_MONTHLY, SENSOR_GAS_YEARLY):
            return {
                "pdr": data.gas_pdr,
                "rate_eur_smc": data.gas_rate,
                "account_number": data.account_number,
            }
        return None
