"""Config flow for Octopus Energy Italy."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import selector

from .api import AuthError, OctopusAccountInfo, OctopusEnergyItalyAPI
from .const import CONF_ACCOUNT_NUMBER, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OctopusEnergyItalyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Octopus Energy Italy.

    Step 1 — credentials: ask for email + password, validate auth
    Step 2 — account:     if multiple accounts, let user pick one
    """

    VERSION = 1

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._accounts: list[OctopusAccountInfo] = []

    # ------------------------------------------------------------------
    # Step 1: credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            # Test credentials and fetch accounts
            api = OctopusEnergyItalyAPI(self._email, self._password, "")
            try:
                self._accounts = await self.hass.async_add_executor_job(
                    api.list_accounts
                )
            except AuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "cannot_connect"

            if not errors:
                if not self._accounts:
                    errors["base"] = "no_accounts"
                elif len(self._accounts) == 1:
                    # Single account — skip picker
                    return self._create_entry(self._accounts[0])
                else:
                    return await self.async_step_account()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL): selector.selector({
                    "text": {"type": "email"}
                }),
                vol.Required(CONF_PASSWORD): selector.selector({
                    "text": {"type": "password"}
                }),
            }),
            errors=errors,
            description_placeholders={
                "portal_url": "https://octopusenergy.it/area-personale"
            },
        )

    # ------------------------------------------------------------------
    # Step 2: account picker (only shown if >1 account)
    # ------------------------------------------------------------------

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            account_number = user_input[CONF_ACCOUNT_NUMBER]
            account = next(
                (a for a in self._accounts if a.account_number == account_number), None
            )
            if account:
                return self._create_entry(account)
            errors["base"] = "unknown"

        account_options = [
            selector.SelectOptionDict(
                value=a.account_number,
                label=f"{a.account_number} — {a.address}"
            )
            for a in self._accounts
        ]

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({
                vol.Required(CONF_ACCOUNT_NUMBER): selector.selector({
                    "select": {"options": account_options}
                }),
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_entry(self, account: OctopusAccountInfo) -> ConfigFlowResult:
        """Create the config entry."""
        # Prevent duplicate entries for the same account
        self._async_abort_entries_match({CONF_ACCOUNT_NUMBER: account.account_number})

        title = f"Octopus {account.account_number}"
        return self.async_create_entry(
            title=title,
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_ACCOUNT_NUMBER: account.account_number,
            },
        )
