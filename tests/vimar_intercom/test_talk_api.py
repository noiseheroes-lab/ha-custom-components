"""Tests for the `vimar_intercom/talk` websocket command's glue.

Home Assistant is not installed for the suite, so the websocket API is
replaced by a stand-in that does what the real decorators and registry
do with a command (see homeassistant/components/websocket_api). The
behaviour behind the command is tested in test_talkback.py; this checks
the wiring: the command name, that it takes no fields, and that it asks
the live hub and the media layer before it opens anything.
"""

import importlib
import sys
import types

import pytest
from test_talkback import FakeConnection

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import talkback as tb


@pytest.fixture
def api(monkeypatch):
    """talk_api imported against a stand-in websocket API."""
    registry = {}

    def websocket_command(schema):
        def decorate(func):
            func._ws_command = schema["type"]
            # As Home Assistant does: a type-only schema is no schema.
            func._ws_schema = False if len(schema) == 1 else schema
            return func
        return decorate

    def async_register_command(hass, handler):
        registry[handler._ws_command] = (handler, handler._ws_schema)

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("homeassistant")
    module("homeassistant.components")
    ws = module("homeassistant.components.websocket_api",
                websocket_command=websocket_command,
                async_register_command=async_register_command,
                ActiveConnection=object)
    module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    sys.modules["homeassistant.components"].websocket_api = ws
    name = "custom_components.vimar_intercom.talk_api"
    monkeypatch.delitem(sys.modules, name, raising=False)
    mod = importlib.import_module(name)
    mod.registry = registry
    yield mod
    sys.modules.pop(name, None)


@pytest.fixture
def source(monkeypatch):
    fresh = tb.TalkbackSource()
    monkeypatch.setattr(media, "talkback", fresh)
    return fresh


def _handler(api, hub, *, sending=True, monkeypatch):
    monkeypatch.setattr(media, "audio_sending", lambda: sending)
    api.async_register_talk_api(object(), lambda _hass: hub)
    handler, schema = api.registry["vimar_intercom/talk"]
    assert schema is False
    return handler


def test_the_command_opens_a_talk_session_during_a_call(
        api, source, monkeypatch):
    handler = _handler(api, types.SimpleNamespace(in_call=True),
                       monkeypatch=monkeypatch)
    conn = FakeConnection()
    handler(None, conn, {"id": 12, "type": "vimar_intercom/talk"})
    assert conn.results == [12]
    assert conn.events[0][1]["type"] == "start"
    assert source.talking


@pytest.mark.parametrize(("hub", "sending", "code"), [
    (None, True, tb.REFUSE_NOT_LOADED),
    (types.SimpleNamespace(in_call=False), True, tb.REFUSE_NO_CALL),
    (types.SimpleNamespace(in_call=True), False, tb.REFUSE_NO_AUDIO),
])
def test_the_command_refuses_without_a_call_carrying_audio(
        api, source, monkeypatch, hub, sending, code):
    handler = _handler(api, hub, sending=sending, monkeypatch=monkeypatch)
    conn = FakeConnection()
    handler(None, conn, {"id": 4, "type": "vimar_intercom/talk"})
    assert conn.errors == [(4, code)]
    assert not source.talking
