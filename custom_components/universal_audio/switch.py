"""Switch entities for Universal Audio Apollo.

Dynamically creates switches for:
- Monitor output: Dim, Mono
- Each discovered input: Mute, Phantom (48V), Pad, Phase, HiPass, LowCut
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .apollo_tcp import ApolloTCPClient, INPUT_BOOL_PROPS
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Icons for input properties
INPUT_PROP_ICONS = {
    "Mute": "mdi:microphone-off",
    "Phantom": "mdi:lightning-bolt",
    "Pad": "mdi:volume-minus",
    "Phase": "mdi:swap-horizontal",
    "HiPass": "mdi:sine-wave",
    "LowCut": "mdi:sine-wave",
    "Polarity": "mdi:swap-vertical",
    "Stereo": "mdi:speaker-multiple",
}

# Friendly names
INPUT_PROP_NAMES = {
    "Phantom": "48V Phantom",
    "HiPass": "High-Pass Filter",
    "LowCut": "Low-Cut Filter",
    "Pad": "Pad (-20 dB)",
    "Phase": "Phase Invert",
    "Polarity": "Polarity Invert",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Apollo switch entities."""
    client: ApolloTCPClient = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = [
        ApolloOutputSwitch(client, entry, "DimOn", "Dim", "mdi:volume-low"),
        ApolloOutputSwitch(client, entry, "MixToMono", "Mono", "mdi:speaker-multiple"),
    ]

    # Wait for enumeration to discover inputs
    # We add input entities after first state callback
    discovered: set[str] = set()

    @callback
    def _check_inputs() -> None:
        new_entities: list[SwitchEntity] = []
        for inp_idx, ch in client.state.inputs.items():
            for prop in INPUT_BOOL_PROPS:
                key = f"input_{inp_idx}_{prop}"
                if key in discovered:
                    continue
                # Only add entities for properties we've actually received
                if prop in ch.properties:
                    discovered.add(key)
                    name_prefix = ch.display_name
                    prop_name = INPUT_PROP_NAMES.get(prop, prop)
                    icon = INPUT_PROP_ICONS.get(prop, "mdi:toggle-switch")
                    new_entities.append(
                        ApolloInputSwitch(
                            client, entry, inp_idx, prop,
                            f"{name_prefix} {prop_name}",
                            icon,
                        )
                    )
        if new_entities:
            async_add_entities(new_entities)

    client.add_callback(_check_inputs)
    async_add_entities(entities)


class ApolloOutputSwitch(SwitchEntity):
    """Boolean toggle on the monitor output (Dim, Mono)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        client: ApolloTCPClient,
        entry: ConfigEntry,
        prop: str,
        name: str,
        icon: str,
    ) -> None:
        self._client = client
        self._prop = prop
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{prop.lower()}"
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
        avail = self._client.state.connected
        return avail

    @property
    def is_on(self) -> bool:
        if self._prop == "DimOn":
            return self._client.state.is_dimmed
        if self._prop == "MixToMono":
            return self._client.state.is_mono
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._client.set_output_bool(self._prop, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._client.set_output_bool(self._prop, False)


class ApolloInputSwitch(SwitchEntity):
    """Boolean toggle on an input channel (Mute, Phantom, Pad, Phase, etc.)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        client: ApolloTCPClient,
        entry: ConfigEntry,
        input_idx: str,
        prop: str,
        name: str,
        icon: str,
    ) -> None:
        self._client = client
        self._input_idx = input_idx
        self._prop = prop
        self._attr_name = name
        self._attr_icon = icon
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
    def is_on(self) -> bool:
        ch = self._client.state.inputs.get(self._input_idx)
        if not ch:
            return False
        val = ch.properties.get(self._prop)
        return self._client._to_bool(val) if val is not None else False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._client.set_input_bool(self._input_idx, self._prop, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._client.set_input_bool(self._input_idx, self._prop, False)
