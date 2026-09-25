"""Vimar Intercom integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from functools import partial

from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from . import media_handler as media
from .const import DOMAIN, ISSUE_MIGRATION_REQUIRED, ISSUE_REGISTRATION_DOWN
from .hub import VimarIntercomHub
from .runtime import RuntimeConfig, build_runtime_config

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor"]

# Key under which the "AV view already registered" marker is kept in
# hass.data[DOMAIN]. Config entry IDs are lowercase alphanumeric ULIDs,
# so this cannot collide with one.
VIEW_REGISTERED = "av_view_registered"

# How long the view waits for the call it triggered to establish.
CALL_SETUP_TIMEOUT = 15.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    cfg = build_runtime_config(entry.data, entry.options)
    hub = VimarIntercomHub(cfg)

    domain_data = hass.data.setdefault(DOMAIN, {})

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    hub.set_issue_callbacks(
        partial(_raise_registration_issue, hass, cfg),
        partial(ir.async_delete_issue, hass, DOMAIN, ISSUE_REGISTRATION_DOWN),
    )

    hub.set_hass(hass, entry.entry_id)
    try:
        await hub.async_start()
    except OSError as err:
        # Almost always the RTP ports being in use. Raising
        # ConfigEntryNotReady gets the user a readable message and an
        # automatic retry, instead of a raw traceback out of setup and a
        # half-populated hass.data.
        await hub.async_stop()
        raise ConfigEntryNotReady(
            f"Could not open the RTP ports "
            f"{cfg.rtp_audio_port}/{cfg.rtp_video_port}: {err}") from err

    domain_data[entry.entry_id] = {"hub": hub}

    # Register the view once per Home Assistant, not once per setup.
    # `HomeAssistantView.register` adds an unnamed aiohttp route, so
    # registering again after a reload leaves two routes on the same
    # path — and aiohttp resolves the first, which is the one holding
    # the torn-down hub.
    if not domain_data.get(VIEW_REGISTERED):
        hass.http.register_view(VimarAVStreamView())
        domain_data[VIEW_REGISTERED] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Refuse to load a 1.x entry, and say what to do about it.

    There is no migration from 1.x: the credentials used to live in
    source and now come from the QR payload, which the integration
    cannot obtain on the user's behalf. Without this handler Home
    Assistant reports "Migration handler not found for entry" and the
    doorbell simply stops, with nothing in the UI explaining why.
    """
    if entry.version < 2:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_MIGRATION_REQUIRED,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_MIGRATION_REQUIRED,
        )
    return False


def _raise_registration_issue(hass: HomeAssistant, cfg: RuntimeConfig) -> None:
    """Tell the user the intercom has been unregistered for too long."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_REGISTRATION_DOWN,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_REGISTRATION_DOWN,
        translation_placeholders={
            "proxy_host": cfg.proxy_host,
            "proxy_port": str(cfg.proxy_port),
        },
    )


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["hub"].async_stop()
        ir.async_delete_issue(hass, DOMAIN, ISSUE_REGISTRATION_DOWN)
    return ok


def _resolve_hub(hass: HomeAssistant) -> VimarIntercomHub | None:
    """Return the live hub, or None if no entry is loaded."""
    for value in hass.data.get(DOMAIN, {}).values():
        if isinstance(value, dict) and "hub" in value:
            return value["hub"]
    return None


class VimarAVStreamView(HomeAssistantView):
    """Serve the intercom audio/video stream as MPEG-TS.

    Reachable only with a Home Assistant bearer token or a signed path;
    the camera entity uses the latter.

    The view holds no hub. It is registered once for the lifetime of
    Home Assistant and resolves the current hub per request, so a config
    entry reload cannot leave it streaming through a hub that no longer
    exists.
    """

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    requires_auth = True

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream MPEG-TS for as long as the client stays connected."""
        hub = _resolve_hub(request.app[KEY_HASS])
        if hub is None:
            return web.Response(status=503, text="Intercom not loaded")

        await hub.stream_opened()
        queue: asyncio.Queue | None = None
        try:
            waited = 0.0
            while not hub.in_call and waited < CALL_SETUP_TIMEOUT:
                await asyncio.sleep(0.5)
                waited += 0.5

            if not hub.in_call:
                _LOGGER.warning("AV stream: call not established after %.0fs",
                                CALL_SETUP_TIMEOUT)
                return web.Response(status=503, text="Call not established")

            queue = await media.av_subscribe()
            if queue is None:
                return web.Response(status=503, text="ffmpeg failed to start")
        finally:
            # Every exit before the stream starts — the 503s included —
            # has to balance the stream_opened above, or the hub keeps
            # counting a viewer that has gone and never clears the
            # auto-call it placed for it.
            if queue is None:
                await hub.stream_closed()

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        try:
            await response.prepare(request)
            # One queue per viewer, fed by the pipeline's single reader:
            # two viewers reading ffmpeg's stdout directly would split
            # the transport stream between them and neither would decode.
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await media.av_unsubscribe(queue)
            await hub.stream_closed()
        return response
