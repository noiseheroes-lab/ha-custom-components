"""Vimar Intercom integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import media_handler as media
from .const import DOMAIN
from .hub import VimarIntercomHub
from .runtime import build_runtime_config

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    cfg = build_runtime_config(entry.data, entry.options)
    hub = VimarIntercomHub(cfg)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub}

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hub.async_start()
    hass.http.register_view(VimarAVStreamView(hub))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["hub"].async_stop()
    return ok


class VimarAVStreamView(HomeAssistantView):
    """Serve the intercom audio/video stream as MPEG-TS.

    Reachable only with a Home Assistant bearer token or a signed path;
    the camera entity uses the latter.
    """

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    requires_auth = True

    def __init__(self, hub: VimarIntercomHub) -> None:
        """Store the hub this view streams from."""
        self._hub = hub

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream MPEG-TS for as long as the client stays connected."""
        await self._hub.stream_opened()

        waited = 0.0
        while not self._hub.in_call and waited < 15:
            await asyncio.sleep(0.5)
            waited += 0.5

        if not self._hub.in_call:
            _LOGGER.warning("AV stream: call not established after 15s")
            await self._hub.stream_closed()
            return web.Response(status=503, text="Call not established")

        await media.start_av_ffmpeg()
        if not media.av_ffmpeg_proc:
            await self._hub.stream_closed()
            return web.Response(status=503, text="ffmpeg failed to start")

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        await response.prepare(request)

        loop = asyncio.get_running_loop()
        try:
            while media.av_ffmpeg_proc and media.av_ffmpeg_proc.poll() is None:
                chunk = await loop.run_in_executor(
                    None, media.av_ffmpeg_proc.stdout.read, 4096)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await media.stop_av_ffmpeg()
            await self._hub.stream_closed()
        return response
