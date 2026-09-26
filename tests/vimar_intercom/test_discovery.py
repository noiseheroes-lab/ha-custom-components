"""Tests for zeroconf discovery of the indoor unit.

`discovery.py` is pure and tested directly. The config flow's discovery
steps are tested against a minimal stand-in for Home Assistant's
`ConfigFlow` — just the unique-ID and abort semantics those steps use —
because the suite runs without Home Assistant installed (see
tests/conftest.py). Every address and MAC here is invented.
"""

import asyncio
import importlib
import sys
import types
from ipaddress import ip_address

import pytest

from custom_components.vimar_intercom import discovery as dz

MAC = "02:00:5E:10:00:01"
TXT = {"mac": "02-00-5e-10-00-01", "proxy": "192.0.2.10",
       "domain": "plant.example.invalid"}


# ─── discovery.py ────────────────────────────────────────────────────

def test_an_announcement_is_parsed_and_its_mac_normalised():
    unit = dz.parse_discovery(
        TXT, "192.0.2.99", "Indoor unit._eipvdes._tcp.local.")
    assert unit == dz.DiscoveredUnit(
        mac=MAC, host="192.0.2.10", name="Indoor unit")


def test_txt_values_may_arrive_as_bytes():
    unit = dz.parse_discovery(
        {b"mac": MAC.encode(), "proxy": b"192.0.2.10"}, "192.0.2.99", "x")
    assert unit.mac == MAC and unit.host == "192.0.2.10"


@pytest.mark.parametrize("props", [
    {}, {"proxy": "192.0.2.10"}, {"mac": ""}, {"mac": "not-a-mac"}])
def test_an_announcement_without_a_valid_mac_is_refused(props):
    with pytest.raises(ValueError):
        dz.parse_discovery(props, "192.0.2.99", "x")


@pytest.mark.parametrize("proxy", [None, "", "bad host\r\n"])
def test_a_missing_or_bad_proxy_falls_back_to_the_sender(proxy):
    props = {"mac": MAC} if proxy is None else {"mac": MAC, "proxy": proxy}
    assert dz.parse_discovery(props, "192.0.2.99", "x").host == "192.0.2.99"


def test_an_existing_entry_is_found_whatever_its_mac_spelling():
    assert dz.configured_unique_id(
        MAC, [None, "60901", "02:00:5e:10:00:01"]) == "02:00:5e:10:00:01"
    assert dz.configured_unique_id(MAC, ["60901", "02:00:5E:10:00:02"]) is None


def test_the_qr_must_carry_the_discovered_mac():
    assert dz.qr_matches_discovery(MAC, "02-00-5E-10-00-01")
    assert not dz.qr_matches_discovery(MAC, "02:00:5E:10:00:02")
    assert not dz.qr_matches_discovery(MAC, "")


# ─── the config flow's discovery steps ───────────────────────────────

class AbortFlow(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class FakeEntry:
    def __init__(self, unique_id, data=None):
        self.unique_id = unique_id
        self.data = dict(data or {})


class FakeConfigFlow:
    """The slice of ConfigFlow the discovery steps rely on."""

    def __init_subclass__(cls, **kwargs):
        pass

    def __init__(self):
        self.context = {}
        self.unique_id = None
        self.entries: list[FakeEntry] = []
        self.hass = None

    def _async_current_entries(self, include_ignore=True):
        return list(self.entries)

    async def async_set_unique_id(self, unique_id, *, raise_on_progress=True):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        for entry in self.entries:
            if entry.unique_id == self.unique_id:
                if updates:
                    entry.data.update(updates)
                self.reloaded = reload_on_update and bool(updates)
                raise AbortFlow("already_configured")

    def async_abort(self, *, reason, **kwargs):
        return {"type": "abort", "reason": reason}

    def async_show_form(self, *, step_id, **kwargs):
        return {"type": "form", "step_id": step_id, **kwargs}

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "data": data}

    def add_suggested_values_to_schema(self, schema, values):
        return schema


@pytest.fixture
def flow_module(monkeypatch):
    """Import config_flow against stand-ins for Home Assistant."""
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    class Schema:
        def __init__(self, *args, **kwargs):
            pass

    module("voluptuous", Schema=Schema, Optional=lambda *a, **k: a[0],
           Required=lambda *a, **k: a[0], All=lambda *a: a[0],
           Range=lambda **k: None)
    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.file_upload",
           process_uploaded_file=None)
    module("homeassistant.config_entries", ConfigEntry=object,
           ConfigFlow=FakeConfigFlow, ConfigFlowResult=dict,
           OptionsFlow=object)
    module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    module("homeassistant.helpers", selector=types.SimpleNamespace(
        FileSelector=lambda c: None, FileSelectorConfig=lambda **k: None,
        TextSelector=lambda c: None, TextSelectorConfig=lambda **k: None))
    module("homeassistant.helpers.service_info")
    module("homeassistant.helpers.service_info.zeroconf",
           ZeroconfServiceInfo=object)
    monkeypatch.delitem(
        sys.modules, "custom_components.vimar_intercom.config_flow",
        raising=False)
    cf = importlib.import_module("custom_components.vimar_intercom.config_flow")
    yield cf
    sys.modules.pop("custom_components.vimar_intercom.config_flow", None)


def _info(props=TXT):
    return types.SimpleNamespace(
        properties=props, ip_address=ip_address("192.0.2.99"),
        name="Indoor unit._eipvdes._tcp.local.")


def _drive(coro):
    try:
        return asyncio.run(coro)
    except AbortFlow as err:
        return {"type": "abort", "reason": err.reason}


def _flow(cf, entries=()):
    # Home Assistant's flow manager, not __init__, gives a flow these.
    flow = cf.VimarIntercomConfigFlow()
    flow.context = {}
    flow.unique_id = None
    flow.hass = None
    flow.entries = list(entries)
    return flow


def test_a_new_unit_is_confirmed_then_asked_for_its_qr(flow_module):
    flow = _flow(flow_module)
    result = _drive(flow.async_step_zeroconf(_info()))
    assert result["step_id"] == "zeroconf_confirm"
    assert flow.unique_id == MAC
    assert flow.context["title_placeholders"] == {
        "name": "Indoor unit", "host": "192.0.2.10"}

    result = _drive(flow.async_step_zeroconf_confirm({}))
    assert result["type"] == "form" and result["step_id"] == "user"


def test_an_already_configured_unit_aborts_and_updates_its_address_quietly(
        flow_module):
    entry = FakeEntry("02:00:5e:10:00:01", {"local_proxy": "192.0.2.1"})
    flow = _flow(flow_module, [entry])
    result = _drive(flow.async_step_zeroconf(_info()))
    assert result == {"type": "abort", "reason": "already_configured"}
    assert entry.data["local_proxy"] == "192.0.2.10"
    assert flow.reloaded is False


def test_an_announcement_missing_its_mac_aborts(flow_module):
    result = _drive(_flow(flow_module).async_step_zeroconf(
        _info({"proxy": "192.0.2.10", "domain": "x"})))
    assert result == {"type": "abort", "reason": "invalid_discovery_info"}


def _qr_for(cf, monkeypatch, mac):
    data = {"mac": mac, "sip_user": "60901", "sip_domain": "example.invalid"}

    async def _entry_data(hass, user_input):
        return dict(data), None

    monkeypatch.setattr(cf, "_async_entry_data_from_form", _entry_data)


def test_a_qr_from_another_unit_aborts_after_discovery(flow_module, monkeypatch):
    flow = _flow(flow_module)
    _drive(flow.async_step_zeroconf(_info()))
    _qr_for(flow_module, monkeypatch, "02:00:5E:10:00:02")
    result = _drive(flow.async_step_user({"qr_payload": "x"}))
    assert result == {"type": "abort", "reason": "discovery_mismatch"}


def test_the_discovered_units_qr_goes_on_to_the_confirm_step(
        flow_module, monkeypatch):
    flow = _flow(flow_module)
    _drive(flow.async_step_zeroconf(_info()))
    _qr_for(flow_module, monkeypatch, "02-00-5e-10-00-01")
    result = _drive(flow.async_step_user({"qr_payload": "x"}))
    assert result["step_id"] == "confirm"
