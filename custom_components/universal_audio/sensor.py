"""Sensor entities for Universal Audio Apollo.

Read-only device information: sample rate, firmware, device name.
"""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .apollo_tcp import ApolloTCPClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Apollo sensor entities."""
    client: ApolloTCPClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ApolloSampleRateSensor(client, entry),
    ])


class ApolloSampleRateSensor(SensorEntity):
    """Current sample rate of the Apollo device."""

    _attr_has_entity_name = True
    _attr_name = "Sample Rate"
    _attr_icon = "mdi:metronome"
    _attr_native_unit_of_measurement = "Hz"

    def __init__(self, client: ApolloTCPClient, entry: ConfigEntry) -> None:
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_sample_rate"
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
    def native_value(self) -> str | None:
        sr = self._client.state.sample_rate
        return sr if sr else None
