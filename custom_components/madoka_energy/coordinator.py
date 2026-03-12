"""Data coordinator for Daikin Madoka Energy."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble_client import MadokaBleClient, MadokaData
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class MadokaCoordinator(DataUpdateCoordinator[MadokaData]):
    """Coordinator to manage BLE polling for Madoka energy data."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = MadokaBleClient(address)
        self.address = address

    async def _async_update_data(self) -> MadokaData:
        try:
            return await self.client.read_data()
        except ConnectionError as err:
            raise UpdateFailed(f"Cannot connect to Madoka {self.address}: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error reading Madoka data: {err}") from err
