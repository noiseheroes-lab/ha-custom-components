"""Vimar Intercom integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import partial

import voluptuous as vol
from aiohttp import web

from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import media_handler as media
from .call_log import CallLog
from .const import (
    ATTR_MESSAGE_ID,
    CA_PATH,
    CALL_LOG_SAVE_DELAY,
    CALL_LOG_STORAGE_KEY,
    CALL_LOG_STORAGE_VERSION,
    DOMAIN,
    ISSUE_MIGRATION_REQUIRED,
    ISSUE_REGISTRATION_DOWN,
    PLANT_STORAGE_KEY,
    PLANT_STORAGE_VERSION,
    SERVICE_CLEAR_MISSED_CALLS,
    SERVICE_DELETE_ALL_VIDEO_MESSAGES,
    SERVICE_DELETE_VIDEO_MESSAGE,
    SERVICE_MARK_VIDEO_MESSAGE_READ,
    SERVICE_PLAY_VIDEO_MESSAGE,
)
from .dashboard_card import async_register_card
from .entity_plan import EntityPlan, plan_entities
from .hub import VimarIntercomHub
from .phonebook import download_phonebook, phonebook_url
from .plant_config import PlantConfig, parse_phonebook
from .runtime import RuntimeConfig, build_runtime_config
from .system_messages import InitStatus
from .talk_api import async_register_talk_api

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor",
             "sensor", "switch", "select"]

# Set up from the UI only. Declared because `async_setup` exists: without
# it a stray `vimar_intercom:` key in configuration.yaml would be
# accepted silently.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Key under which the "AV view already registered" marker is kept in
# hass.data[DOMAIN]. Config entry IDs are lowercase alphanumeric ULIDs,
# so this cannot collide with one.
VIEW_REGISTERED = "av_view_registered"

# How long the view waits for the call it triggered to establish.
CALL_SETUP_TIMEOUT = 15.0


_MESSAGE_SCHEMA = vol.Schema({vol.Required(ATTR_MESSAGE_ID): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard card, its talk-back command and the services.

    Once per run: here rather than in `async_setup_entry`, which runs
    again on every reload; see `dashboard_card`. The talk-back command
    resolves the hub per request, so it outlives reloads the way the AV
    view does. The services exist whether or not an entry is loaded, as
    Home Assistant recommends, and say so when none is.
    """
    await async_register_card(hass)
    async_register_talk_api(hass, _resolve_hub)
    _register_services(hass)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """The video-message and call-log services.

    Each resolves the live hub per call, so a reload never leaves one
    acting through a hub that has been torn down, and raises the hub's
    own reason when it fails: a service that only logs looks identical
    to one that worked.
    """

    def _hub() -> VimarIntercomHub:
        hub = _resolve_hub(hass)
        if hub is None:
            raise HomeAssistantError("Vimar Intercom is not loaded.")
        return hub

    def _checked(result: tuple[bool, str]) -> None:
        ok, msg = result
        if not ok:
            raise HomeAssistantError(msg)

    async def _mark_read(call: ServiceCall) -> None:
        _checked(await _hub().async_mark_video_message_read(
            call.data[ATTR_MESSAGE_ID]))

    async def _delete(call: ServiceCall) -> None:
        _checked(await _hub().async_delete_video_message(
            call.data[ATTR_MESSAGE_ID]))

    async def _delete_all(call: ServiceCall) -> None:
        _checked(await _hub().async_delete_all_video_messages())

    async def _play(call: ServiceCall) -> None:
        _checked(await _hub().async_play_video_message(
            call.data[ATTR_MESSAGE_ID]))

    async def _clear_missed(call: ServiceCall) -> None:
        await _hub().async_clear_missed_calls()

    hass.services.async_register(
        DOMAIN, SERVICE_MARK_VIDEO_MESSAGE_READ, _mark_read, _MESSAGE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_VIDEO_MESSAGE, _delete, _MESSAGE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_DELETE_ALL_VIDEO_MESSAGES, _delete_all, vol.Schema({}))
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_VIDEO_MESSAGE, _play, _MESSAGE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_MISSED_CALLS, _clear_missed, vol.Schema({}))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    if not await hass.async_add_executor_job(os.path.exists, CA_PATH):
        # The SIP connection carries the door command and the digest
        # response. Without this file its certificate cannot be verified,
        # and the integration used to carry on unverified rather than
        # say so. Refusing is the honest answer, and ConfigEntryNotReady
        # retries on its own once the file is back.
        raise ConfigEntryNotReady(
            f"The Vimar CA certificate is missing from {CA_PATH}. "
            "Reinstall the integration through HACS: the connection to "
            "the intercom cannot be verified without it.")

    # The last plant configuration the phonebook produced, so the
    # entities exist — with the installer's names — from the first
    # second, even with the cloud unreachable. Without one the options
    # decide, exactly as before the phonebook was read at all.
    store = _plant_store(hass, entry.entry_id)
    plant = await _async_load_plant(store)
    cfg = build_runtime_config(entry.data, entry.options, plant)
    plan = plan_entities(cfg, plant)
    hub = VimarIntercomHub(cfg)

    domain_data = hass.data.setdefault(DOMAIN, {})

    # A v2 entry is loading, so whatever 1.x entry raised the
    # re-setup issue has been dealt with.
    ir.async_delete_issue(hass, DOMAIN, ISSUE_MIGRATION_REQUIRED)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    hub.set_issue_callbacks(
        partial(_raise_registration_issue, hass, cfg),
        partial(ir.async_delete_issue, hass, DOMAIN, ISSUE_REGISTRATION_DOWN),
    )

    hub.set_hass(hass, entry.entry_id)
    # The call log survives restarts; a damaged store gives an empty log
    # rather than a failed setup (`CallLog.from_dict`).
    log_store = _call_log_store(hass, entry.entry_id)
    call_log = CallLog.from_dict(await log_store.async_load())
    hub.set_call_log(call_log, partial(
        log_store.async_delay_save, call_log.to_dict, CALL_LOG_SAVE_DELAY))
    # Before the hub starts: its first registration asks for the status.
    # A changed configuration reloads the entry rather than adding and
    # removing entities in place: the plan, the runtime config the SIP
    # layer holds (default panel, panel names on ring events) and the
    # registry cleanup below are then all derived once, from one plant,
    # the same way a restart derives them. It happens on the first
    # download and when the installer changes the plant — rarely enough
    # that the few seconds of re-registration do not matter — and the
    # hub holds it back while a call is up.
    hub.set_plant_sync(
        plant,
        partial(_async_fetch_plant, hass, cfg),
        partial(_async_save_plant, store),
        partial(hass.config_entries.async_schedule_reload, entry.entry_id),
    )
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

    domain_data[entry.entry_id] = {"hub": hub, "plan": plan}
    _remove_stale_entities(hass, entry, plan)

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


def _plant_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The per-entry store of the last good plant configuration."""
    return Store(hass, PLANT_STORAGE_VERSION,
                 PLANT_STORAGE_KEY.format(entry_id=entry_id))


def _call_log_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The per-entry store of the local call log."""
    return Store(hass, CALL_LOG_STORAGE_VERSION,
                 CALL_LOG_STORAGE_KEY.format(entry_id=entry_id))


async def _async_load_plant(store: Store) -> PlantConfig | None:
    """The stored plant configuration, or None if there is no usable one."""
    data = await store.async_load()
    if data is None:
        return None
    try:
        return PlantConfig.from_dict(data)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring the stored plant configuration (%s); the panels from "
            "the integration options are used until the phonebook is "
            "downloaded again", err)
        return None


async def _async_save_plant(
    store: Store, _previous: PlantConfig | None, plant: PlantConfig
) -> None:
    """Persist a new plant configuration. It never contains the token."""
    await store.async_save(plant.to_dict())


async def _async_fetch_plant(
    hass: HomeAssistant, cfg: RuntimeConfig, status: InitStatus, group: str
) -> PlantConfig:
    """Download the phonebook `status` names and parse it off the loop.

    `status.token` is the download password. It goes into the digest
    computation and nowhere else.
    """
    assert status.rubrica_ver is not None and status.token is not None
    url = phonebook_url(cfg.cloud_proxy, cfg.sip_domain, status.rubrica_ver)
    data = await download_phonebook(
        async_get_clientsession(hass), url, cfg.sip_domain, status.token)
    return await hass.async_add_executor_job(
        partial(parse_phonebook, data, group=group,
                version=status.rubrica_ver))


def _remove_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, plan: EntityPlan
) -> None:
    """Remove the registry entries of panels and actuators that are gone.

    Runs before the platforms add their entities, so it never races
    them. Only the plant-dependent families are considered (see
    `entity_plan.DYNAMIC_PREFIXES`); the camera, the sensors and the
    other fixed entities are never touched.
    """
    registry = er.async_get(hass)
    by_unique_id = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    for unique_id in plan.stale_unique_ids(entry.entry_id, by_unique_id):
        _LOGGER.info("Removing %s: the plant no longer has it",
                     by_unique_id[unique_id])
        registry.async_remove(by_unique_id[unique_id])


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored plant configuration and call log with the entry."""
    await _plant_store(hass, entry.entry_id).async_remove()
    await _call_log_store(hass, entry.entry_id).async_remove()


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
    else:
        # An entry from a newer version of the integration than the one
        # installed: the user has downgraded, and this code cannot know
        # what that entry contains. Refusing is right, but saying so is
        # the difference between a readable cause and a bare failure.
        _LOGGER.error(
            "Config entry version %s was written by a newer version of "
            "Vimar Intercom than the one installed, which cannot read it. "
            "Upgrade the integration again, or delete the entry and set "
            "it up from the QR code.", entry.version)
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

        queue: asyncio.Queue | None = None
        # Inside the try, so a cancellation between opening the stream
        # and reaching the body cannot leave the hub counting a viewer
        # that has gone. `stream_opened` gives the count back itself if
        # it raises, so `opened` only becomes True once it is owed.
        opened = False
        try:
            await hub.stream_opened()
            opened = True
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
            if queue is None and opened:
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
