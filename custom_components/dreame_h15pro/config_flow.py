"""Config flow for Dreame H15 Pro."""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DreameAuthError, DreameCloudAPI
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_DID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class DreameH15ProConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Dreame H15 Pro."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: DreameCloudAPI | None = None
        self._devices: list[dict[str, Any]] = []
        self._refresh_token: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle the initial step - enter refresh token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input[CONF_REFRESH_TOKEN].strip()
            self._refresh_token = refresh_token

            session = async_get_clientsession(self.hass)
            api = DreameCloudAPI(
                session=session,
                access_token="",
                refresh_token=refresh_token,
                token_expiry=0,  # Force immediate refresh
            )

            try:
                await api._refresh_access_token()
                self._api = api
                devices = await api.get_devices()
                # Filter to hold devices only
                self._devices = [
                    d for d in devices if "hold" in d.get("model", "")
                ]
                if not self._devices:
                    self._devices = devices  # Show all if no hold devices
            except DreameAuthError:
                errors["base"] = "auth_failed"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "connection_error"
            else:
                if not self._devices:
                    errors["base"] = "no_devices"
                elif len(self._devices) == 1:
                    return await self._create_entry(self._devices[0])
                else:
                    return await self.async_step_select_device()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_REFRESH_TOKEN): str}
            ),
            errors=errors,
        )

    async def async_step_select_device(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle device selection."""
        if user_input is not None:
            did = user_input[CONF_DEVICE_DID]
            device = next(
                (d for d in self._devices if d["did"] == did), None
            )
            if device:
                return await self._create_entry(device)

        device_options = {
            d["did"]: f"{d.get('customName', d['model'])} ({d['did']})"
            for d in self._devices
        }

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_DID): vol.In(device_options),
                }
            ),
        )

    async def _create_entry(self, device: dict[str, Any]) -> dict[str, Any]:
        """Create config entry for selected device."""
        did = device["did"]
        await self.async_set_unique_id(did)
        self._abort_if_unique_id_configured()

        assert self._api is not None
        return self.async_create_entry(
            title=device.get("customName", device["model"]),
            data={
                CONF_REFRESH_TOKEN: self._api.refresh_token,
                CONF_ACCESS_TOKEN: self._api.access_token,
                CONF_TOKEN_EXPIRY: self._api.token_expiry,
                CONF_DEVICE_DID: did,
                CONF_DEVICE_NAME: device.get("customName", "H15 Pro"),
                CONF_DEVICE_MODEL: device.get("model", "dreame.hold.w2448e"),
            },
        )
