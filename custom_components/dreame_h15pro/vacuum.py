"""Vacuum entity for Dreame H15 Pro."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    PROP_BATTERY,
    PROP_CLEAN_AREA,
    PROP_CLEAN_TIME,
    PROP_SUCTION_LEVEL,
    PROP_WATER_TEMP,
    STATUS_DISPLAY,
)
from .coordinator import DreameH15ProCoordinator

_LOGGER = logging.getLogger(__name__)

# Map internal status to HA vacuum states
_STATUS_TO_HA = {
    "mopping": "cleaning",
    "vacuuming": "cleaning",
    "self_cleaning": "cleaning",
    "hot_water_self_cleaning": "cleaning",
    "drying": "docked",
    "charging": "docked",
    "charging_complete": "docked",
    "sleeping": "docked",
    "standby": "idle",
    "adding_water": "idle",
    "mopping_paused": "paused",
    "vacuuming_paused": "paused",
    "self_cleaning_paused": "paused",
    "drying_paused": "paused",
    "offline": "error",
    "updating": "docked",
    "updating_voice": "docked",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up vacuum from config entry."""
    coordinator: DreameH15ProCoordinator = hass.data[DOMAIN][entry.entry_id]
    did = entry.data[CONF_DEVICE_DID]
    name = entry.data.get(CONF_DEVICE_NAME, "H15 Pro")
    model = entry.data.get(CONF_DEVICE_MODEL, "dreame.hold.w2448e")

    async_add_entities([DreameH15ProVacuum(coordinator, did, name, model)])


class DreameH15ProVacuum(
    CoordinatorEntity[DreameH15ProCoordinator], StateVacuumEntity
):
    """Dreame H15 Pro vacuum entity."""

    _attr_has_entity_name = True
    _attr_name = None  # Use device name
    _attr_supported_features = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.RETURN_HOME
    )

    def __init__(
        self,
        coordinator: DreameH15ProCoordinator,
        did: str,
        name: str,
        model: str,
    ) -> None:
        super().__init__(coordinator)
        self._did = did
        self._attr_unique_id = f"{did}_vacuum"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": name,
            "manufacturer": "Dreame",
            "model": model,
        }

    @property
    def state(self) -> str | None:
        if self.coordinator.data is None:
            return None
        status = self.coordinator.data.get("status", "unknown")
        return _STATUS_TO_HA.get(status, "idle")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        attrs = {
            "status": self.coordinator.data.get("status"),
            "status_display": STATUS_DISPLAY.get(
                self.coordinator.data.get("status", ""), ""
            ),
            "status_code": self.coordinator.data.get("status_code"),
        }
        # Add cleaning info
        clean_time = self.coordinator.data.get(PROP_CLEAN_TIME)
        if clean_time is not None:
            attrs["clean_time_min"] = int(clean_time)
        clean_area = self.coordinator.data.get(PROP_CLEAN_AREA)
        if clean_area is not None:
            attrs["clean_area_m2"] = round(float(clean_area), 1)
        suction = self.coordinator.data.get(PROP_SUCTION_LEVEL)
        if suction is not None:
            attrs["suction_level"] = suction
        water_temp = self.coordinator.data.get(PROP_WATER_TEMP)
        if water_temp is not None:
            attrs["water_temp_c"] = int(water_temp)
        battery = self.coordinator.data.get(PROP_BATTERY)
        if battery is not None:
            attrs["battery_level"] = int(battery)
        return attrs

    async def async_start(self) -> None:
        """Start mopping."""
        await self.coordinator.api.start_clean(self._did)
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop and return to standby."""
        await self.coordinator.api.stop(self._did)
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        """Pause current operation."""
        await self.coordinator.api.pause(self._did)
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Return to charging dock."""
        await self.coordinator.api.set_property(self._did, 2, 1, 4)
        await self.coordinator.async_request_refresh()

    # ── Custom services ──────────────────────────────────────────────

    async def async_start_vacuum(self) -> None:
        """Start vacuuming (custom service)."""
        await self.coordinator.api.start_vacuum(self._did)
        await self.coordinator.async_request_refresh()

    async def async_start_self_clean(self) -> None:
        """Start self-cleaning (custom service)."""
        await self.coordinator.api.start_self_clean(self._did)
        await self.coordinator.async_request_refresh()

    async def async_start_drying(self) -> None:
        """Start drying (custom service)."""
        await self.coordinator.api.start_drying(self._did)
        await self.coordinator.async_request_refresh()
