"""Diagnostics for Vimar Intercom.

A diagnostics download is meant to be attached to a public bug report,
so it carries no secret and no identity: the SIP password, the device
identity and push token, the SIP user and domain, the panel's MAC and
LAN address, the apartment group and the installer's panel names are
all redacted, and the hub's part (`VimarIntercomHub.diagnostics`) is
counts, flags and the settings the indoor unit reported.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_UUID,
    CONF_GROUP_ID,
    CONF_LOCAL_PROXY,
    CONF_MAC,
    CONF_PANELS,
    CONF_PUSH_TOKEN,
    CONF_SIP_DOMAIN,
    CONF_SIP_PASSWORD,
    CONF_SIP_USER,
    DOMAIN,
)

TO_REDACT = {
    CONF_SIP_PASSWORD,
    CONF_PUSH_TOKEN,
    CONF_DEVICE_ID,
    CONF_DEVICE_UUID,
    CONF_SIP_USER,
    CONF_SIP_DOMAIN,
    CONF_MAC,
    CONF_LOCAL_PROXY,
    CONF_GROUP_ID,
    CONF_PANELS,
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the redacted entry and the hub's state."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    hub = data.get("hub") if isinstance(data, dict) else None
    return {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "hub": hub.diagnostics() if hub is not None else None,
    }
