"""Select entities for Dreame H15 Pro."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DOMAIN,
    PROP_CLEANING_MODE,
    PROP_SELF_CLEAN_MODE,
)
from .coordinator import DreameH15ProCoordinator

_LOGGER = logging.getLogger(__name__)

# Cleaning mode options (1.49) — mapped from live values
CLEANING_MODE_OPTIONS = {
    "Eco": 0,
    "Standard": 1,
    "Forte": 2,
    "Turbo": 3,
    "Auto": 11,
}

# Self-clean mode (1.51 values)
SELF_CLEAN_MODE_OPTIONS = {
    "Rapida": 1,
    "Standard": 2,
    "Profonda": 3,
    "Acqua calda": 4,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities from config entry."""
    coordinator: DreameH15ProCoordinator = hass.data[DOMAIN][entry.entry_id]
    did = entry.data[CONF_DEVICE_DID]
    name = entry.data.get(CONF_DEVICE_NAME, "H15 Pro")
    model = entry.data.get(CONF_DEVICE_MODEL, "dreame.hold.w2448e")

    entities = [
        DreameSelectEntity(
            coordinator, did, name, model,
            key="cleaning_mode",
            prop_key=PROP_CLEANING_MODE,
            siid=1, piid=49,
            entity_name="Modalita pulizia",
            icon="mdi:broom",
            options_map=CLEANING_MODE_OPTIONS,
        ),
        DreameSelectEntity(
            coordinator, did, name, model,
            key="self_clean_mode",
            prop_key=PROP_SELF_CLEAN_MODE,
            siid=1, piid=51,
            entity_name="Modalita autopulizia",
            icon="mdi:washing-machine",
            options_map=SELF_CLEAN_MODE_OPTIONS,
        ),
    ]
    async_add_entities(entities)


class DreameSelectEntity(
    CoordinatorEntity[DreameH15ProCoordinator], SelectEntity
):
    """Select entity for Dreame H15 Pro mode controls."""

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
        options_map: dict[str, int],
    ) -> None:
        super().__init__(coordinator)
        self._did = did
        self._prop_key = prop_key
        self._siid = siid
        self._piid = piid
        self._options_map = options_map
        self._reverse_map = {v: k for k, v in options_map.items()}
        self._attr_name = entity_name
        self._attr_icon = icon
        self._attr_unique_id = f"{did}_{key}"
        self._attr_options = list(options_map.keys())
        self._attr_device_info = {
            "identifiers": {(DOMAIN, did)},
            "name": device_name,
            "manufacturer": "Dreame",
            "model": model,
        }

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        raw = self.coordinator.data.get(self._prop_key)
        if raw is None:
            return None
        try:
            val = int(raw)
        except (ValueError, TypeError):
            return None
        return self._reverse_map.get(val)

    async def async_select_option(self, option: str) -> None:
        value = self._options_map.get(option)
        if value is None:
            return
        await self.coordinator.api.set_property(
            self._did, self._siid, self._piid, value
        )
        await self.coordinator.async_request_refresh()
