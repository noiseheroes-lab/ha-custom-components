"""Octopus Energy Italy integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import AuthError, OctopusEnergyItalyAPI
from .const import CONF_ACCOUNT_NUMBER, DATA_COORDINATOR, DOMAIN
from .coordinator import OctopusEnergyCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octopus Energy Italy from a config entry."""
    email: str = entry.data[CONF_EMAIL]
    password: str = entry.data[CONF_PASSWORD]
    account_number: str = entry.data[CONF_ACCOUNT_NUMBER]

    api = OctopusEnergyItalyAPI(email, password, account_number)

    # Verify credentials on startup
    try:
        await hass.async_add_executor_job(api.authenticate)
    except AuthError as e:
        raise ConfigEntryAuthFailed(f"Invalid credentials: {e}") from e
    except Exception as e:
        raise ConfigEntryNotReady(f"Cannot connect to Octopus API: {e}") from e

    coordinator = OctopusEnergyCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_COORDINATOR: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
