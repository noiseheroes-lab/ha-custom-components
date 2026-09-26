"""The `vimar_intercom/talk` websocket command: talk-back's way in.

The card streams microphone audio over the websocket it already holds,
as binary messages to a handler registered for the request — the
mechanism Assist uses for its audio pipeline. No new HTTP endpoint, no
token handling in the card: the websocket is authenticated before any
command reaches it, and closing it ends the stream. Everything that
decides anything lives in `talkback.py`; this is the Home Assistant
glue, kept as thin as it can be.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import media_handler as media
from .const import DOMAIN
from .talkback import start_talk, talk_refusal

WS_TYPE_TALK = f"{DOMAIN}/talk"


@callback
def async_register_talk_api(
    hass: HomeAssistant, resolve_hub: Callable[[HomeAssistant], Any]
) -> None:
    """Register the command, once per Home Assistant run.

    `resolve_hub` finds the live hub per request, as the AV view does, so
    an entry reload cannot leave the command talking to a stopped hub.
    """

    # The schema is the type alone: the request carries nothing else, so
    # no validator library is needed and nothing can be smuggled in.
    @websocket_api.websocket_command({"type": WS_TYPE_TALK})
    @callback
    def ws_talk(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Give this connection the panel's speaker for the call."""
        hub = resolve_hub(hass)
        refusal = talk_refusal(
            loaded=hub is not None,
            in_call=bool(hub is not None and hub.in_call),
            sending=media.audio_sending(),
        )
        start_talk(connection, msg["id"], media.talkback, refusal)

    websocket_api.async_register_command(hass, ws_talk)
