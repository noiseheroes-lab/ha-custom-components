"""The dashboard card: served from this package, loaded on every dashboard.

The card is one ES module in `frontend/`, with no build step. Home
Assistant serves it from a static path and adds it to every frontend
page as an extra module, so installing the integration is all a user
does: no Lovelace resource to add, no HACS frontend repository, and
the card always matches the entities it reads, because it ships in the
same release.

Registration is once per Home Assistant run. `async_setup` is not
called again when an entry reloads, and the marker in `hass.data`
covers anything else that might call this twice: aiohttp refuses a
second route on the same path, and the frontend would load the module
twice.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "vimar-intercom-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / CARD_FILENAME
# Outside /api, like every static path: nothing here needs a token, and
# the file holds no data, only the card's code.
CARD_URL = f"/{DOMAIN}/frontend/{CARD_FILENAME}"

# Key of the "card already registered" marker in hass.data[DOMAIN].
# Config entry IDs are lowercase alphanumeric ULIDs, so it cannot
# collide with one.
CARD_REGISTERED = "card_registered"


def card_module_url(version: str | None) -> str:
    """The URL the frontend loads, with the release as cache buster.

    Browsers keep a module for as long as its URL is unchanged, so an
    update that kept the URL would leave every open dashboard running
    the previous card against the new entities.
    """
    return f"{CARD_URL}?v={version or 'dev'}"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and have the frontend load it, once per run.

    A failure is logged, not raised. The card is a convenience; the
    doorbell, the door and the camera must set up without it.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(CARD_REGISTERED):
        return
    # Set before the first await, so two callers racing cannot both
    # reach the router.
    domain_data[CARD_REGISTERED] = True
    try:
        integration = await async_get_integration(hass, DOMAIN)
        # No long-lived cache headers: the version in the URL only
        # changes with a release, and a development install updated
        # without one would otherwise be stuck on the old card for a
        # year. Revalidating it costs a 304.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_FILE), False)])
        add_extra_js_url(hass, card_module_url(integration.version))
    except Exception:  # noqa: BLE001 - the card is optional, the doorbell is not
        _LOGGER.warning(
            "Could not register the Vimar Intercom dashboard card; the "
            "integration works without it", exc_info=True)
