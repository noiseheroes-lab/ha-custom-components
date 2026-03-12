"""Config flow for Daikin Madoka Energy."""

from __future__ import annotations

import logging

import voluptuous as vol
from bleak import BleakScanner
from homeassistant.config_entries import ConfigFlow

from .const import CONF_DEVICE_ADDRESS, DOMAIN, UART_SERVICE_UUID

_LOGGER = logging.getLogger(__name__)


class MadokaEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Madoka Energy."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_devices: dict[str, str] = {}

    async def async_step_user(self, user_input=None):
        """Handle user-initiated config flow."""
        errors = {}

        if user_input is not None:
            address = user_input[CONF_DEVICE_ADDRESS]
            await self.async_set_unique_id(address.lower())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Madoka {address[-8:]}",
                data={CONF_DEVICE_ADDRESS: address},
            )

        # Scan for Madoka devices
        devices = await BleakScanner.discover(timeout=10)
        self._discovered_devices = {}
        for device in devices:
            if device.name and "madoka" in device.name.lower():
                self._discovered_devices[device.address] = (
                    f"{device.name} ({device.address})"
                )
            elif device.metadata and "uuids" in device.metadata:
                for uuid in device.metadata["uuids"]:
                    if UART_SERVICE_UUID.lower() in uuid.lower():
                        self._discovered_devices[device.address] = (
                            f"{device.name or 'Madoka'} ({device.address})"
                        )
                        break

        if not self._discovered_devices:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required(CONF_DEVICE_ADDRESS): str}
                ),
                errors={"base": "no_devices_found"} if not errors else errors,
                description_placeholders={
                    "hint": "No Madoka devices found. Enter the MAC address manually."
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ADDRESS): vol.In(
                        self._discovered_devices
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_bluetooth(self, discovery_info):
        """Handle Bluetooth discovery."""
        address = discovery_info.address
        await self.async_set_unique_id(address.lower())
        self._abort_if_unique_id_configured()

        self.context["title_placeholders"] = {
            "name": discovery_info.name or f"Madoka {address[-8:]}"
        }

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input=None):
        """Confirm Bluetooth discovery."""
        if user_input is not None:
            return self.async_create_entry(
                title=self.context["title_placeholders"]["name"],
                data={CONF_DEVICE_ADDRESS: self.unique_id},
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )
