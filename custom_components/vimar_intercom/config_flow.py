"""Config flow for Vimar Intercom."""

from homeassistant import config_entries

from .const import DOMAIN


class VimarIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vimar Intercom."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="Vimar Intercom", data={})

        return self.async_show_form(step_id="user")
