"""Number entities for Universal Audio Apollo.

Dynamically creates number sliders for:
- Each discovered input: Gain (0-65 dB)
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .apollo_tcp import ApolloTCPClient, INPUT_FLOAT_PROPS
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Apollo number entities."""
    client: ApolloTCPClient = hass.data[DOMAIN][entry.entry_id]

    discovered: set[str] = set()

    @callback
    def _check_inputs() -> None:
        new_entities: list[NumberEntity] = []
        for inp_idx, ch in client.state.inputs.items():
            for prop in INPUT_FLOAT_PROPS:
                key = f"input_{inp_idx}_{prop}"
                if key in discovered:
                    continue
                if prop in ch.properties:
                    discovered.add(key)
                    name = f"{ch.display_name} {prop}"
                    new_entities.append(
                        ApolloInputGain(client, entry, inp_idx, prop, name)
                    )
        if new_entities:
            async_add_entities(new_entities)

    client.add_callback(_check_inputs)


class ApolloInputGain(NumberEntity):
    """Gain control for an input channel."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:knob"
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0.0
    _attr_native_max_value = 65.0
    _attr_native_step = 0.5

    def __init__(
        self,
        client: ApolloTCPClient,
        entry: ConfigEntry,
        input_idx: str,
        prop: str,
        name: str,
    ) -> None:
        self._client = client
        self._input_idx = input_idx
        self._prop = prop
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_input_{input_idx}_{prop.lower()}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    async def async_added_to_hass(self) -> None:
        self._client.add_callback(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        self._client.remove_callback(self._state_changed)

    @callback
    def _state_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.state.connected

    @property
    def native_value(self) -> float | None:
        ch = self._client.state.inputs.get(self._input_idx)
        if not ch:
            return None
        val = ch.properties.get(self._prop)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self._client.set_input_float(self._input_idx, self._prop, value)
