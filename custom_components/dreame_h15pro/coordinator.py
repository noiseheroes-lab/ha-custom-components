"""Data update coordinator for Dreame H15 Pro."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DreameApiError, DreameAuthError, DreameCloudAPI
from .const import (
    ALL_PROPS,
    CLEANING_STATUSES,
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_DID,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CLEANING_FINISHED,
    EVENT_CLEANING_STARTED,
    EVENT_SELF_CLEAN_FINISHED,
    EVENT_SELF_CLEAN_STARTED,
    PROP_CLEAN_AREA,
    PROP_CLEAN_TIME,
    PROP_STATUS,
    PROP_SUCTION_LEVEL,
    PROP_WORK_MODE,
    STATUS_MAP,
)

_LOGGER = logging.getLogger(__name__)

# Statuses that count as "actively cleaning"
_ACTIVE_CLEANING = {"mopping", "vacuuming"}
_SELF_CLEANING = {"self_cleaning", "hot_water_self_cleaning"}


class DreameH15ProCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to poll Dreame Cloud API for device data."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, api: DreameCloudAPI, did: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self.did = did
        self._prev_status: str | None = None
        self._session_start: float | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Dreame Cloud."""
        try:
            props = await self.api.get_props(self.did)
        except DreameAuthError as err:
            try:
                await self.api._refresh_access_token()
                self._persist_tokens()
                props = await self.api.get_props(self.did)
            except DreameAuthError:
                raise UpdateFailed(f"Authentication failed: {err}") from err
        except DreameApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

        # Persist refreshed tokens
        self._persist_tokens()

        # Parse values
        data: dict[str, Any] = {}
        for key, raw_value in props.items():
            try:
                if key == PROP_STATUS:
                    val = int(raw_value)
                    data["status_code"] = val
                    data["status"] = STATUS_MAP.get(val, f"unknown_{val}")
                elif isinstance(raw_value, str) and raw_value.startswith("["):
                    data[key] = raw_value
                else:
                    data[key] = self._parse_value(raw_value)
            except (ValueError, TypeError):
                data[key] = raw_value

        # Fire session events on status transitions
        self._track_sessions(data)

        return data

    def _track_sessions(self, data: dict[str, Any]) -> None:
        """Fire HA events on cleaning session start/end transitions."""
        current_status = data.get("status")
        prev = self._prev_status
        self._prev_status = current_status

        if prev is None or current_status == prev:
            return

        now = time.time()

        # Cleaning started
        if current_status in _ACTIVE_CLEANING and prev not in _ACTIVE_CLEANING:
            self._session_start = now
            self.hass.bus.async_fire(
                EVENT_CLEANING_STARTED,
                {
                    "mode": current_status,
                    "suction_level": data.get(PROP_SUCTION_LEVEL),
                    "work_mode": data.get(PROP_WORK_MODE),
                },
            )

        # Cleaning ended
        elif prev in _ACTIVE_CLEANING and current_status not in _ACTIVE_CLEANING:
            duration = round(now - self._session_start) if self._session_start else None
            self.hass.bus.async_fire(
                EVENT_CLEANING_FINISHED,
                {
                    "mode": prev,
                    "duration_sec": duration,
                    "clean_time_min": data.get(PROP_CLEAN_TIME),
                    "clean_area_m2": data.get(PROP_CLEAN_AREA),
                    "ended_with": current_status,
                },
            )
            self._session_start = None

        # Self-clean started
        elif current_status in _SELF_CLEANING and prev not in _SELF_CLEANING:
            self._session_start = now
            self.hass.bus.async_fire(
                EVENT_SELF_CLEAN_STARTED,
                {"mode": current_status},
            )

        # Self-clean ended
        elif prev in _SELF_CLEANING and current_status not in _SELF_CLEANING:
            duration = round(now - self._session_start) if self._session_start else None
            self.hass.bus.async_fire(
                EVENT_SELF_CLEAN_FINISHED,
                {
                    "mode": prev,
                    "duration_sec": duration,
                    "ended_with": current_status,
                },
            )
            self._session_start = None

    @staticmethod
    def _parse_value(raw: str) -> int | float | str:
        """Parse a property value string."""
        try:
            val = int(raw)
            return val
        except (ValueError, TypeError):
            pass
        try:
            val = float(raw)
            return val
        except (ValueError, TypeError):
            pass
        return raw

    def _persist_tokens(self) -> None:
        """Persist updated tokens to config entry."""
        new_data = {
            **self.config_entry.data,
            CONF_ACCESS_TOKEN: self.api.access_token,
            CONF_REFRESH_TOKEN: self.api.refresh_token,
            CONF_TOKEN_EXPIRY: self.api.token_expiry,
        }
        if new_data != dict(self.config_entry.data):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
