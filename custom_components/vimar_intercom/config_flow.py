"""Config, reconfigure and options flows for Vimar Intercom."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_UUID,
    CONF_DOOR_COMMAND,
    CONF_MAC,
    CONF_PANELS,
    CONF_PREFER_LOCAL,
    CONF_PUSH_TOKEN,
    CONF_RTP_PORT_BASE,
    CONF_SIP_DOMAIN,
    CONF_SIP_PORT,
    CONF_SIP_USER,
    DEFAULT_DOOR_COMMAND,
    DEFAULT_PANELS,
    DEFAULT_RTP_PORT_BASE,
    DEFAULT_SIP_PORT,
    DOMAIN,
)
from .qr import (
    MAX_IMAGE_BYTES,
    QRImageUnreadableError,
    QRInputError,
    choose_qr_input,
    decode_qr,
    read_qr_image,
)
from .runtime import entry_data_from_qr, parse_panels, valid_door_command

_LOGGER = logging.getLogger(__name__)

CONF_QR_PAYLOAD = "qr_payload"
CONF_QR_IMAGE = "qr_image"

_QR_SCHEMA = vol.Schema({
    # Most people hold the QR as a picture, not as text: the indoor unit
    # only shows it on screen. Both fields are optional in the schema and
    # choose_qr_input decides which one counts.
    vol.Optional(CONF_QR_IMAGE): selector.FileSelector(
        selector.FileSelectorConfig(accept="image/*")
    ),
    vol.Optional(CONF_QR_PAYLOAD): selector.TextSelector(
        selector.TextSelectorConfig(multiline=True)
    ),
})


def _read_uploaded_qr(hass: HomeAssistant, file_id: str) -> str:
    """Read an uploaded QR image and return the text it encodes.

    Runs in the executor, as process_uploaded_file requires, so that its
    teardown does not run on the loop either. Leaving the `with` deletes
    the upload: the image is the credentials, and nothing needs it once
    its bytes are in memory. Pillow and zbar work on those bytes after
    the file is gone.
    """
    try:
        with process_uploaded_file(hass, file_id) as path, path.open("rb") as fh:
            # One byte over the limit is enough for read_qr_image to refuse
            # it, without pulling the rest of a huge upload into memory.
            data = fh.read(MAX_IMAGE_BYTES + 1)
    except (ValueError, OSError) as err:
        # ValueError: the id is unknown, for example after a restart
        # or a second submit of the same upload.
        raise QRImageUnreadableError from err
    return read_qr_image(data)


def _qr_form_schema(flow: ConfigFlow, user_input: dict[str, Any] | None) -> vol.Schema:
    """The QR form, with the text the user last pasted suggested back.

    Rebuilding it empty on `invalid_qr` threw away a long base64 blob the
    user had to find and paste again to see the same error. It is a
    suggestion, not a default: a default would be put back by the schema
    when the user clears the field, and a stale paste would then be
    decoded instead of the "nothing given" error. The image cannot be
    offered back; its upload is deleted as soon as it has been read.
    """
    pasted = (user_input or {}).get(CONF_QR_PAYLOAD)
    return flow.add_suggested_values_to_schema(
        _QR_SCHEMA, {CONF_QR_PAYLOAD: pasted} if pasted else None)


async def _async_entry_data_from_form(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Turn the QR form into entry data, or into the error key to show.

    Whichever field is used, the text goes through the same decode_qr
    and entry_data_from_qr as a paste always has, so a QR read from an
    image gets every check a pasted one does.
    """
    try:
        source, value = choose_qr_input(
            user_input.get(CONF_QR_IMAGE), user_input.get(CONF_QR_PAYLOAD))
        if source == "image":
            value = await hass.async_add_executor_job(
                _read_uploaded_qr, hass, value)
        # `entry_data_from_qr` validates too, and raises the same
        # ValueError QRDecodeError already is, so both the decoding and
        # the field checks fail the same way here.
        return entry_data_from_qr(decode_qr(value)), None
    except QRInputError as err:
        # The class name only: the exception chain may hold Pillow's
        # view of the image.
        _LOGGER.debug("QR input rejected: %s", type(err).__name__)
        return None, err.error_key
    except ValueError as err:
        _LOGGER.debug("QR payload rejected: %s", err)
        return None, "invalid_qr"


class VimarIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the integration from the QR code the indoor unit generates."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._entry_data: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the QR code, as an image or as text, and decode it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data, error = await _async_entry_data_from_form(self.hass, user_input)
            if data is None:
                errors["base"] = error or "invalid_qr"
            else:
                await self.async_set_unique_id(
                    data[CONF_MAC] or data[CONF_SIP_USER])
                self._abort_if_unique_id_configured()
                self._entry_data = data
                return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user",
            data_schema=_qr_form_schema(self, user_input),
            errors=errors)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found and create the entry on confirmation."""
        assert self._entry_data is not None

        if user_input is not None:
            return self.async_create_entry(
                title="Vimar Intercom", data=self._entry_data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=_summary_placeholders(self._entry_data),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the credentials of an existing entry with a fresh QR."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            data, error = await _async_entry_data_from_form(self.hass, user_input)
            if data is None:
                errors["base"] = error or "invalid_qr"
            else:
                # Keep the identity generated at first setup: the Vimar
                # cloud tracks the registration by it.
                for key in (CONF_DEVICE_ID, CONF_DEVICE_UUID, CONF_PUSH_TOKEN):
                    data[key] = entry.data.get(key, data[key])
                await self.async_set_unique_id(
                    data[CONF_MAC] or data[CONF_SIP_USER])
                self._abort_if_unique_id_mismatch(reason="wrong_panel")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_qr_form_schema(self, user_input),
            errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return VimarIntercomOptionsFlow()


class VimarIntercomOptionsFlow(OptionsFlow):
    """Tune the things a user may want to change after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                parse_panels(user_input[CONF_PANELS])
            except ValueError as err:
                _LOGGER.debug("Panel list rejected: %s", err)
                errors[CONF_PANELS] = "invalid_panels"
            # This string is sent as the body of a SIP MESSAGE. Free text
            # there is how a CRLF, or a non-ASCII character the
            # Content-Length would then mis-count, gets onto the wire.
            if not valid_door_command(user_input[CONF_DOOR_COMMAND]):
                _LOGGER.debug("Door command rejected")
                errors[CONF_DOOR_COMMAND] = "invalid_door_command"
            if not errors:
                return self.async_create_entry(data=user_input)

        # Redisplay what the user actually submitted, falling back to the
        # saved options. Rebuilding the form from the saved options alone
        # would silently discard every other edit they made alongside the
        # one that failed validation.
        current = {**self.config_entry.options, **(user_input or {})}

        schema = vol.Schema({
            vol.Required(
                CONF_PANELS,
                default=current.get(CONF_PANELS, DEFAULT_PANELS),
            ): str,
            vol.Required(
                CONF_DOOR_COMMAND,
                default=current.get(CONF_DOOR_COMMAND, DEFAULT_DOOR_COMMAND),
            ): str,
            vol.Required(
                CONF_PREFER_LOCAL,
                default=current.get(CONF_PREFER_LOCAL, False),
            ): bool,
            vol.Required(
                CONF_SIP_PORT,
                default=current.get(CONF_SIP_PORT, DEFAULT_SIP_PORT),
            ): vol.All(int, vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_RTP_PORT_BASE,
                default=current.get(CONF_RTP_PORT_BASE, DEFAULT_RTP_PORT_BASE),
            ): vol.All(int, vol.Range(min=1024, max=50000)),
        })

        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors)


def _summary_placeholders(data: dict[str, Any]) -> dict[str, str]:
    """Describe what the QR contained, with the password masked."""
    return {
        "sip_user": data[CONF_SIP_USER],
        "sip_domain": data[CONF_SIP_DOMAIN],
        "mac": data[CONF_MAC] or "not provided",
        "password": "•" * 8,
    }
