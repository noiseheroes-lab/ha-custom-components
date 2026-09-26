"""Tests for what the dashboard card depends on.

The card sees only what Home Assistant sends the frontend: entity IDs,
the registry's display entries and the states. It tells a door lock
from a light, and one panel's call button from another's, by the
`intercom_role`, `panel` and `default_panel` attributes, so those are a
contract; renaming one breaks every dashboard silently. The card must
also be served and loaded exactly once per Home Assistant run.

The platforms import Home Assistant, which the suite does not install,
so they are imported against minimal stand-ins, as test_discovery does
for the config flow.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

from custom_components.vimar_intercom import runtime
from custom_components.vimar_intercom.entity_plan import ButtonPlan, LockPlan

ENTRY = "01JTESTENTRY"
PLATFORMS = ("button", "lock", "camera", "event", "binary_sensor")
PANELS = (runtime.PanelConfig("55001", "Front gate"),
          runtime.PanelConfig("55002", "Lobby"))


class _Entity:
    """Home Assistant's Entity, as far as these tests look at it."""

    @property
    def extra_state_attributes(self):
        return getattr(self, "_attr_extra_state_attributes", None)


def _install_ha_stubs(monkeypatch):
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    enum = types.SimpleNamespace
    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.button",
           ButtonEntity=type("ButtonEntity", (_Entity,), {}))
    module("homeassistant.components.lock",
           LockEntity=type("LockEntity", (_Entity,), {}))
    module("homeassistant.components.camera",
           Camera=type("Camera", (_Entity,), {}),
           CameraEntityFeature=enum(STREAM=2))
    module("homeassistant.components.event",
           EventEntity=type("EventEntity", (_Entity,), {}),
           EventDeviceClass=enum(DOORBELL="doorbell"))
    module("homeassistant.components.binary_sensor",
           BinarySensorEntity=type("BinarySensorEntity", (_Entity,), {}),
           BinarySensorDeviceClass=enum(CONNECTIVITY="connectivity"))
    module("homeassistant.components.http")
    module("homeassistant.components.http.auth", async_sign_path=None)
    module("homeassistant.config_entries", ConfigEntry=object)
    module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    module("homeassistant.exceptions",
           HomeAssistantError=type("HomeAssistantError", (Exception,), {}))
    module("homeassistant.helpers")
    module("homeassistant.helpers.device_registry", DeviceInfo=dict)
    module("homeassistant.helpers.entity",
           EntityCategory=enum(CONFIG="config"))
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=None)
    module("homeassistant.helpers.network", get_url=None)
    return module


@pytest.fixture
def platforms(monkeypatch):
    """The five platform modules, imported against the stand-ins."""
    _install_ha_stubs(monkeypatch)
    names = [f"custom_components.vimar_intercom.{p}" for p in PLATFORMS]
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield types.SimpleNamespace(
        **{p: importlib.import_module(n) for p, n in zip(PLATFORMS, names, strict=True)})
    for name in names:
        sys.modules.pop(name, None)


class FakeHub:
    """A hub with the one thing the entities read at construction."""

    def __init__(self, panels=PANELS):
        self.config = types.SimpleNamespace(
            panels=panels, default_panel=panels[0],
            proxy_host="sip.example.invalid", proxy_port=7042)


def _role(entity) -> str:
    return entity.extra_state_attributes["intercom_role"]


# ─── the attributes the card reads ───────────────────────────────────

def test_every_fixed_entity_states_its_role(platforms):
    hub = FakeHub()
    b, bs = platforms.button, platforms.binary_sensor
    roles = {
        _role(platforms.camera.VimarIntercomCamera(hub, ENTRY)),
        _role(platforms.event.VimarDoorbellEvent(hub, ENTRY)),
        _role(bs.VimarSIPRegistrationSensor(hub, ENTRY)),
        _role(bs.VimarInCallSensor(hub, ENTRY)),
        _role(b.VimarAnswerButton(hub, ENTRY)),
        _role(b.VimarHangupButton(hub, ENTRY)),
        _role(b.VimarReconnectButton(hub, ENTRY)),
    }
    assert roles == {"camera", "doorbell", "registration", "in_call",
                     "answer", "hangup", "reconnect"}


def test_panel_buttons_name_their_panel(platforms):
    b = platforms.button
    call = b.VimarCallButton(FakeHub(), ENTRY, PANELS[1])
    door = b.VimarDoorButton(FakeHub(), ENTRY, PANELS[1])
    assert call.extra_state_attributes == {
        "intercom_role": "call", "panel": "55002", "panel_name": "Lobby"}
    assert door.extra_state_attributes == {
        "intercom_role": "open", "panel": "55002", "panel_name": "Lobby"}


def test_buttons_do_not_share_one_attribute_dict(platforms):
    # The role is a class attribute; the dict it goes into must not be,
    # or the second panel's button would relabel the first one's.
    b = platforms.button
    first = b.VimarCallButton(FakeHub(), ENTRY, PANELS[0])
    b.VimarCallButton(FakeHub(), ENTRY, PANELS[1])
    b.VimarAnswerButton(FakeHub(), ENTRY)
    assert first.extra_state_attributes["panel"] == "55001"


def test_actuators_are_actuators_and_locks_are_doors(platforms):
    light = platforms.button.VimarActuatorButton(
        FakeHub(), ENTRY,
        ButtonPlan("actuator_55001_AUX6", "Garden light", "55001", "AUX6",
                   "LIGHT"))
    generic = platforms.lock.VimarIntercomLock(
        FakeHub(), ENTRY, LockPlan("lock", None, None, None))
    gate = platforms.lock.VimarIntercomLock(
        FakeHub(), ENTRY,
        LockPlan("actuator_55002_OPEN_2F", "Lobby door", "55002", "OPEN_2F"))
    assert light.extra_state_attributes == {"intercom_role": "actuator"}
    assert _role(generic) == _role(gate) == "door"


def test_the_camera_names_the_panel_its_stream_calls(platforms):
    camera = platforms.camera.VimarIntercomCamera(FakeHub(), ENTRY)
    attrs = camera.extra_state_attributes
    assert attrs["default_panel"] == "55001"
    assert attrs["default_panel_name"] == "Front gate"


def test_the_registration_sensor_keeps_its_proxy_attributes(platforms):
    sensor = platforms.binary_sensor.VimarSIPRegistrationSensor(
        FakeHub(), ENTRY)
    assert sensor.extra_state_attributes == {
        "intercom_role": "registration",
        "proxy_host": "sip.example.invalid",
        "proxy_port": "7042",
    }


# ─── serving and loading the card ────────────────────────────────────

class FakeHTTP:
    def __init__(self, fail=False):
        self.registered = []
        self.fail = fail

    async def async_register_static_paths(self, configs):
        if self.fail:
            raise RuntimeError("route already registered")
        self.registered.extend(configs)


@pytest.fixture
def card(monkeypatch):
    """dashboard_card imported against stand-ins, recording JS URLs."""
    module = _install_ha_stubs(monkeypatch)
    js_urls = []

    class StaticPathConfig:
        def __init__(self, url_path, path, cache_headers=True):
            self.url_path, self.path = url_path, path
            self.cache_headers = cache_headers

    async def async_get_integration(hass, domain):
        return types.SimpleNamespace(version="9.8.7")

    module("homeassistant.components.http", StaticPathConfig=StaticPathConfig)
    module("homeassistant.components.frontend",
           add_extra_js_url=lambda hass, url: js_urls.append(url))
    module("homeassistant.loader", async_get_integration=async_get_integration)
    name = "custom_components.vimar_intercom.dashboard_card"
    monkeypatch.delitem(sys.modules, name, raising=False)
    mod = importlib.import_module(name)
    mod.js_urls = js_urls
    yield mod
    sys.modules.pop(name, None)


def _hass(fail=False):
    return types.SimpleNamespace(data={}, http=FakeHTTP(fail))


def test_the_card_is_served_and_loaded_with_the_release_as_cache_buster(card):
    hass = _hass()
    asyncio.run(card.async_register_card(hass))
    [static] = hass.http.registered
    assert static.url_path == "/vimar_intercom/frontend/vimar-intercom-card.js"
    assert Path(static.path).is_file()
    assert card.js_urls == [
        "/vimar_intercom/frontend/vimar-intercom-card.js?v=9.8.7"]


def test_the_card_is_registered_once_per_run(card):
    hass = _hass()
    asyncio.run(card.async_register_card(hass))
    asyncio.run(card.async_register_card(hass))
    assert len(hass.http.registered) == 1
    assert len(card.js_urls) == 1


def test_a_failed_registration_does_not_fail_setup(card):
    hass = _hass(fail=True)
    asyncio.run(card.async_register_card(hass))  # must not raise
    assert card.js_urls == []


def test_the_marker_cannot_be_mistaken_for_a_hub(card):
    # `_resolve_hub` walks hass.data[DOMAIN] looking for dicts with a hub.
    hass = _hass()
    asyncio.run(card.async_register_card(hass))
    assert not isinstance(hass.data["vimar_intercom"]["card_registered"], dict)


def test_the_card_module_defines_the_custom_element(card):
    source = card.CARD_FILE.read_text(encoding="utf-8")
    assert "customElements.define" in source
    assert '"vimar-intercom-card"' in source
    # Self-contained: nothing fetched from a CDN, no module to resolve.
    assert not any(line.lstrip().startswith("import ")
                   for line in source.splitlines())
    assert "import(" not in source
