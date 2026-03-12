"""Media player entity for Universal Audio Apollo.

Exposes volume and mute as standard media_player controls.
All discovered input/output properties exposed as extra state attributes.
"""
from __future__ import annotations

import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .apollo_tcp import ApolloTCPClient
from .const import ATTR_DIM, ATTR_MONO, ATTR_VOLUME_DB, ATTR_DEVICE_ONLINE, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Apollo media player."""
    client: ApolloTCPClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ApolloMediaPlayer(client, entry)])


class ApolloMediaPlayer(MediaPlayerEntity):
    """Media player representing the Apollo monitor output."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:speaker"
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_STEP
    )

    def __init__(self, client: ApolloTCPClient, entry: ConfigEntry) -> None:
        self._client = client
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_monitor"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Apollo",
            "manufacturer": "Universal Audio",
            "model": "Apollo Solo",
        }

    async def async_added_to_hass(self) -> None:
        self._client.add_callback(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        self._client.remove_callback(self._state_changed)

    @callback
    def _state_changed(self) -> None:
        if self._client.state.device_name != "Apollo":
            self._attr_device_info["name"] = self._client.state.device_name
        self.async_write_ha_state()

    @property
    def state(self) -> MediaPlayerState:
        if not self._client.state.connected:
            return MediaPlayerState.OFF
        if self._client.state.is_muted:
            return MediaPlayerState.IDLE
        return MediaPlayerState.ON

    @property
    def available(self) -> bool:
        return self._client.state.connected

    @property
    def volume_level(self) -> float | None:
        if not self._client.state.connected:
            return None
        return self._client.state.volume_normalized

    @property
    def is_volume_muted(self) -> bool | None:
        if not self._client.state.connected:
            return None
        return self._client.state.is_muted

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            ATTR_VOLUME_DB: round(self._client.state.volume_db, 1),
            ATTR_DIM: self._client.state.is_dimmed,
            ATTR_MONO: self._client.state.is_mono,
            ATTR_DEVICE_ONLINE: self._client.state.device_online,
        }

        # Expose discovered input channel info
        for inp_idx, ch in self._client.state.inputs.items():
            prefix = f"input_{inp_idx}"
            attrs[f"{prefix}_name"] = ch.display_name
            for prop, value in ch.properties.items():
                attrs[f"{prefix}_{prop.lower()}"] = value

        # Sample rate and firmware
        if self._client.state.sample_rate:
            attrs["sample_rate"] = self._client.state.sample_rate
        if self._client.state.firmware:
            attrs["firmware"] = self._client.state.firmware

        return attrs

    async def async_set_volume_level(self, volume: float) -> None:
        await self._client.set_volume(volume)

    async def async_mute_volume(self, mute: bool) -> None:
        await self._client.set_mute(mute)

    async def async_volume_up(self) -> None:
        current = self._client.state.volume_normalized
        await self._client.set_volume(min(1.0, current + 0.03))

    async def async_volume_down(self) -> None:
        current = self._client.state.volume_normalized
        await self._client.set_volume(max(0.0, current - 0.03))
