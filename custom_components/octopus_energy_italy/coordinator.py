"""DataUpdateCoordinator for Octopus Energy Italy."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import APIError, AuthError, OctopusData, OctopusEnergyItalyAPI
from .const import DOMAIN, UPDATE_INTERVAL_HOURS

_LOGGER = logging.getLogger(__name__)


class OctopusEnergyCoordinator(DataUpdateCoordinator[OctopusData]):
    """Coordinator that polls Kraken API every 6 hours."""

    def __init__(self, hass: HomeAssistant, api: OctopusEnergyItalyAPI) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self.api = api

    async def _async_update_data(self) -> OctopusData:
        """Fetch data from Kraken API."""
        try:
            return await self.hass.async_add_executor_job(self.api.fetch_data)
        except AuthError as e:
            raise UpdateFailed(f"Authentication failed: {e}") from e
        except APIError as e:
            raise UpdateFailed(f"API error: {e}") from e
