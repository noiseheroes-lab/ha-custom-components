"""Config flow for Universal Audio Apollo."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_DEVICE_INDEX,
    CONF_OUTPUT_INDEX,
    DEFAULT_PORT,
    DEFAULT_DEVICE_INDEX,
    DEFAULT_OUTPUT_INDEX,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class UniversalAudioConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Universal Audio Apollo."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle user step — enter host and port."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input.get(CONF_PORT, DEFAULT_PORT)

            # Test TCP connection
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=5
                )
                writer.close()
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Apollo ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_DEVICE_INDEX: user_input.get(
                            CONF_DEVICE_INDEX, DEFAULT_DEVICE_INDEX
                        ),
                        CONF_OUTPUT_INDEX: user_input.get(
                            CONF_OUTPUT_INDEX, DEFAULT_OUTPUT_INDEX
                        ),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(
                        CONF_DEVICE_INDEX, default=DEFAULT_DEVICE_INDEX
                    ): int,
                    vol.Optional(
                        CONF_OUTPUT_INDEX, default=DEFAULT_OUTPUT_INDEX
                    ): int,
                }
            ),
            errors=errors,
        )
