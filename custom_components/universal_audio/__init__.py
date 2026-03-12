"""Universal Audio Apollo integration for Home Assistant.

Connects to UA Console via TCP (port 4710) and exposes
the monitor output as a media_player + switch entities.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .apollo_tcp import ApolloTCPClient
from .const import (
    CONF_DEVICE_INDEX,
    CONF_HOST,
    CONF_OUTPUT_INDEX,
    CONF_PORT,
    DEFAULT_DEVICE_INDEX,
    DEFAULT_OUTPUT_INDEX,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.SWITCH, Platform.NUMBER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Universal Audio Apollo from a config entry."""
    _LOGGER.warning("Setting up Universal Audio Apollo: %s:%s", entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT))
    try:
        client = ApolloTCPClient(
            host=entry.data[CONF_HOST],
            port=entry.data.get(CONF_PORT, DEFAULT_PORT),
            device_index=entry.data.get(CONF_DEVICE_INDEX, DEFAULT_DEVICE_INDEX),
            output_index=entry.data.get(CONF_OUTPUT_INDEX, DEFAULT_OUTPUT_INDEX),
        )

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

        await client.connect()
        _LOGGER.warning("Apollo client connected, forwarding platforms")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.warning("Apollo setup complete")
    except Exception:
        _LOGGER.exception("Apollo setup failed")
        return False

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    client: ApolloTCPClient = hass.data[DOMAIN][entry.entry_id]
    await client.disconnect()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
