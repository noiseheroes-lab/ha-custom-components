"""Dreame H15 Pro integration for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DreameCloudAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_DID,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
)
from .coordinator import DreameH15ProCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.VACUUM,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dreame H15 Pro from a config entry."""
    session = async_get_clientsession(hass)

    api = DreameCloudAPI(
        session=session,
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        token_expiry=entry.data[CONF_TOKEN_EXPIRY],
    )

    did = entry.data[CONF_DEVICE_DID]
    coordinator = DreameH15ProCoordinator(hass, api, did)
    coordinator.config_entry = entry

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom services
    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
    """Register custom services for the integration."""

    async def _get_vacuum(call: ServiceCall):
        """Find the vacuum entity from a service call."""
        entity_id = call.data.get("entity_id")
        if not entity_id:
            return None
        state = hass.states.get(entity_id)
        if state is None:
            return None
        # Find the coordinator
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items():
            return coordinator
        return None

    async def handle_self_clean(call: ServiceCall) -> None:
        """Handle self_clean service."""
        coordinator = await _get_vacuum(call)
        if coordinator:
            await coordinator.api.start_self_clean(coordinator.did)
            await coordinator.async_request_refresh()

    async def handle_start_drying(call: ServiceCall) -> None:
        """Handle start_drying service."""
        coordinator = await _get_vacuum(call)
        if coordinator:
            await coordinator.api.start_drying(coordinator.did)
            await coordinator.async_request_refresh()

    async def handle_start_vacuum(call: ServiceCall) -> None:
        """Handle start_vacuum service."""
        coordinator = await _get_vacuum(call)
        if coordinator:
            await coordinator.api.start_vacuum(coordinator.did)
            await coordinator.async_request_refresh()

    if not hass.services.has_service(DOMAIN, "self_clean"):
        hass.services.async_register(DOMAIN, "self_clean", handle_self_clean)
    if not hass.services.has_service(DOMAIN, "start_drying"):
        hass.services.async_register(DOMAIN, "start_drying", handle_start_drying)
    if not hass.services.has_service(DOMAIN, "start_vacuum"):
        hass.services.async_register(DOMAIN, "start_vacuum", handle_start_vacuum)
