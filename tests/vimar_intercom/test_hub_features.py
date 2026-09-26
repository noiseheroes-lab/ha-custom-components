"""Tests for the hub's native-app features.

Do-not-disturb, the answering machine and its timeout, the ring and the
call log, the camera switch and the video-message mailbox — each driven
through the same entry points the SIP layer uses (`_handle_broadcast`)
with the SIP operations replaced by recorders. Every address, name and
ID is invented.
"""

import asyncio
import base64
import sqlite3

import pytest

from custom_components.vimar_intercom import call_log as cl
from custom_components.vimar_intercom import hub, runtime
from custom_components.vimar_intercom import plant_config as pc
from custom_components.vimar_intercom import sip_client as sip

QR_FIELDS = {"ID": "60901", "PWD": "examplepassword", "CDOMAIN": "example.invalid"}
PLANT = pc.PlantConfig(
    version="0" * 32, group=pc.Group("21", "Flat", "55001"),
    panels=(pc.Panel("55001", "Front gate"), pc.Panel("55002", "Lobby")),
    actuators=(),
    system=(("MAGIC_APT_INTERCOM", "21"), ("VM_PREFIX", "90")))
PICG = "sip:60001@example.invalid"
SGA = "sip:21@example.invalid"


def run(coro):
    return asyncio.run(coro)


class Bus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type, data):
        self.events.append((event_type, data))


class Rig:
    """A hub with a plant, a recording bus and a recording SIP layer."""

    def __init__(self, monkeypatch, plant=PLANT):
        cfg = runtime.build_runtime_config(
            runtime.entry_data_from_qr(QR_FIELDS),
            {"panels": "55001:Front gate,55002:Lobby"}, plant)
        self.hub = hub.VimarIntercomHub(cfg)
        self.hub._plant = plant
        self.bus = Bus()
        self.hub.set_hass(type("H", (), {"bus": self.bus})(), "entry1")
        self.sent: list[tuple[str, str, dict | None]] = []
        self.calls: list[str] = []
        self.updates = 0
        self.saves = 0
        self.reply: str | None = None
        self.hub.register_update_callback(self._updated)
        self.hub.set_call_log(cl.CallLog(), self._saved)
        rig = self

        monkeypatch.setattr(sip, "in_call", False)
        monkeypatch.setattr(sip, "calling", False)
        monkeypatch.setattr(sip, "is_registered", lambda: True)
        monkeypatch.setattr(sip, "pending_incoming", dict(sip.pending_incoming))
        monkeypatch.setattr(sip, "call_state", dict(sip.call_state))

        async def _send(uri, body, extra_headers=None):
            rig.sent.append((uri, body, extra_headers))
            if rig.reply is not None and body.startswith("SET_APT_PARAMS;"):
                msg_id = body.split('"MSGID":"')[1].split('"')[0]
                line = rig.reply.replace("<id>", msg_id)
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(rig.message(line)))
            return True, "OK (200)"

        async def _call(target=None):
            rig.calls.append(f"call:{target}")
            return True, "Connected"

        async def _answer():
            rig.calls.append("answer")
            sip.pending_incoming["active"] = False
            monkeypatch.setattr(sip, "in_call", True)
            return True, "Answered"

        async def _decline(busy=False):
            rig.calls.append("decline:486" if busy else "decline:603")
            sip.pending_incoming["active"] = False

        monkeypatch.setattr(sip, "do_system_message", _send)
        monkeypatch.setattr(sip, "do_call", _call)
        monkeypatch.setattr(sip, "do_answer_incoming", _answer)
        monkeypatch.setattr(sip, "do_decline_incoming", _decline)

    def _updated(self):
        self.updates += 1

    def _saved(self):
        self.saves += 1

    async def message(self, body, panda="blue", koala=None):
        await self.hub._handle_broadcast("message", sip.InboundMessage(
            sender=PICG, panda=panda, body=body, koala=koala))

    async def ring(self, panel="55001", call_ids=("ring-1@pbx", "abcde12345")):
        sip.pending_incoming.update(
            active=True, cid=call_ids[0], caller_uri=f"sip:{panel}@example.invalid",
            call_ids=call_ids)
        await self.hub._handle_broadcast("ring", "Incoming call")

    def events(self, name):
        return [data for kind, data in self.bus.events if kind == name]


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


# ─── do-not-disturb and the answering machine ────────────────────────

STATUS = ('GET_INIT_STATUS_REPLY;[{"PARAM":"dnd","VALUE":"1"},'
          '{"PARAM":"voicemail","VALUE":"0"},{"PARAM":"vm_timeout","VALUE":30},'
          '{"PARAM":"vm_timeout_values","VALUE":[15,30,60]},'
          '{"PARAM":"vm_level","VALUE":"2/20"}]')


def test_the_status_reply_sets_the_apartment_settings(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message(STATUS))
    apt = rig.hub.apartment
    assert (apt.dnd, apt.voicemail, apt.vm_timeout) == (True, False, 30)
    assert apt.vm_timeout_values == (15, 30, 60)
    assert rig.hub.mailbox_usage == (2, 20)
    assert rig.updates >= 1


@pytest.mark.parametrize(("method", "on", "body"), [
    ("async_set_dnd", True, "DND;ON"),
    ("async_set_dnd", False, "DND;OFF"),
    ("async_set_voicemail", True, "VOICEMAIL;ON"),
])
def test_a_switch_goes_to_the_apartment_intercom_and_is_optimistic(
        monkeypatch, method, on, body):
    rig = Rig(monkeypatch)
    ok, _ = run(getattr(rig.hub, method)(on))
    assert ok
    assert rig.sent == [(SGA, body, {"Panda": "blue"})]
    attr = "dnd" if "dnd" in method else "voicemail"
    assert getattr(rig.hub.apartment, attr) is on


def test_without_an_apartment_intercom_nothing_is_sent(monkeypatch):
    plant = pc.PlantConfig(version="0" * 32, group=None, panels=(), actuators=())
    rig = Rig(monkeypatch, plant=plant)
    ok, msg = run(rig.hub.async_set_dnd(True))
    assert not ok and "apartment intercom" in msg
    assert rig.sent == []
    assert rig.hub.apartment.dnd is None


def test_the_units_notifications_update_the_switches(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message("DND;ON\nVOICEMAIL;ON"))
    assert rig.hub.apartment.dnd is True
    assert rig.hub.apartment.voicemail is True
    run(rig.message("DND;OFF"))
    assert rig.hub.apartment.dnd is False


def test_the_timeout_is_confirmed_by_its_reply(monkeypatch):
    rig = Rig(monkeypatch)
    rig.reply = 'SET_APT_PARAMS_REPLY;{"MSGID":"<id>","ERRCODE":"ERR_NONE"}'
    ok, _ = run(rig.hub.async_set_vm_timeout(60))
    assert ok
    uri, body, headers = rig.sent[0]
    assert uri == PICG and headers == {"Panda": "set"}
    assert '"PARAM":"vm_timeout","VALUE":60' in body
    assert rig.hub.apartment.vm_timeout == 60


def test_a_refused_timeout_names_the_error_code(monkeypatch):
    rig = Rig(monkeypatch)
    rig.reply = 'SET_APT_PARAMS_REPLY;{"MSGID":"<id>","ERRCODE":"ERR_INVALID_VALUE"}'
    ok, msg = run(rig.hub.async_set_vm_timeout(60))
    assert not ok and "ERR_INVALID_VALUE" in msg
    assert rig.hub.apartment.vm_timeout is None


def test_a_reply_for_another_request_does_not_confirm(monkeypatch):
    rig = Rig(monkeypatch)
    rig.reply = 'SET_APT_PARAMS_REPLY;{"MSGID":"someone","ERRCODE":"ERR_NONE"}'
    monkeypatch.setattr(hub, "APT_PARAMS_TIMEOUT", 0.1)
    ok, msg = run(rig.hub.async_set_vm_timeout(60))
    assert not ok and "did not confirm" in msg


def test_a_timeout_the_unit_does_not_offer_is_refused_locally(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message(STATUS))
    rig.sent.clear()
    ok, _ = run(rig.hub.async_set_vm_timeout(45))
    assert not ok and rig.sent == []


def test_another_device_changing_the_timeout_is_followed(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message('APT_PARAMS_CHANGED;{"PARAM":"vm_timeout","VALUE":15}'))
    assert rig.hub.apartment.vm_timeout == 15


# ─── the ring ────────────────────────────────────────────────────────

def test_a_ring_is_on_until_the_panel_cancels_and_then_missed(monkeypatch):
    rig = Rig(monkeypatch)
    monkeypatch.setattr(cl, "UNANSWERED_GRACE", 0.05)

    async def scenario():
        await rig.ring()
        assert rig.hub.ringing.panel == "55001"
        assert rig.hub.ringing.panel_name == "Front gate"
        await rig.hub._handle_broadcast("ring_ended", sip.RingEnded(
            call_id="ring-1@pbx", ours=True, answered_elsewhere=False))
        assert rig.hub.ringing is None
        assert rig.events("vimar_intercom_missed_call") == []
        await asyncio.sleep(0.1)

    run(scenario())
    (missed,) = rig.events("vimar_intercom_missed_call")
    assert missed["panel"] == "55001" and missed["panel_name"] == "Front gate"
    assert rig.hub.call_log.missed_count == 1
    assert rig.saves >= 2


def test_a_cancel_saying_answered_elsewhere_is_not_missed(monkeypatch):
    rig = Rig(monkeypatch)

    async def scenario():
        await rig.ring()
        await rig.hub._handle_broadcast("ring_ended", sip.RingEnded(
            call_id="ring-1@pbx", ours=True, answered_elsewhere=True))

    run(scenario())
    assert rig.hub.ringing is None
    assert rig.hub.call_log.entries[0].outcome == "answered_elsewhere"


def test_the_units_answered_notice_ends_the_ring(monkeypatch):
    rig = Rig(monkeypatch)

    async def scenario():
        await rig.ring()
        await rig.message("C;someone-else;ANSWERED")
        assert rig.hub.ringing is not None
        await rig.message("C;abcde12345;ANSWERED")

    run(scenario())
    assert rig.hub.ringing is None
    assert rig.hub.call_log.entries[0].outcome == "answered_elsewhere"


def test_answering_tells_the_other_devices_with_the_first_call_id(monkeypatch):
    rig = Rig(monkeypatch)

    async def scenario():
        await rig.ring()
        ok, _ = await rig.hub.async_answer()
        assert ok
        await _settle()

    run(scenario())
    assert rig.hub.ringing is None
    assert (SGA, "C;ring-1@pbx;ANSWERED", {"Panda": "blue"}) in rig.sent
    assert rig.hub.call_log.entries[0].outcome == "answered"


def test_declining_ends_the_ring_with_603(monkeypatch):
    rig = Rig(monkeypatch)

    async def scenario():
        await rig.ring()
        await rig.hub.async_decline()

    run(scenario())
    assert "decline:603" in rig.calls
    assert rig.hub.ringing is None
    assert rig.hub.call_log.entries[0].outcome == "declined"


def test_a_ring_nothing_ends_times_out(monkeypatch):
    rig = Rig(monkeypatch)
    monkeypatch.setattr(hub, "RING_TIMEOUT", 0.05)
    monkeypatch.setattr(cl, "UNANSWERED_GRACE", 10)

    async def scenario():
        await rig.ring()
        await asyncio.sleep(0.1)
        assert rig.hub.ringing is None

    run(scenario())


def test_a_ring_during_a_call_is_logged_but_does_not_ring(monkeypatch):
    rig = Rig(monkeypatch)
    monkeypatch.setattr(sip, "in_call", True)

    async def scenario():
        await rig.ring(panel="55002")
        await _settle()

    run(scenario())
    assert "decline:486" in rig.calls
    assert rig.hub.ringing is None
    assert rig.hub.call_log.entries[0].panel == "55002"


def test_the_units_missed_call_fires_once(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message('MISSED_CALL;{"SIP_ID":"55002","TS":"1700000000"}'))
    (event,) = rig.events("vimar_intercom_missed_call")
    assert event["panel"] == "55002" and event["panel_name"] == "Lobby"
    assert event["time"].startswith("2023-11-14T")
    assert rig.hub.call_log.missed_count == 1
    run(rig.hub.async_clear_missed_calls())
    assert rig.hub.call_log.missed_count == 0


def test_a_missed_call_with_a_hostile_panel_id_is_logged_as_unknown(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message('MISSED_CALL;{"SIP_ID":"55 01<x>","TS":"1"}'))
    assert rig.hub.call_log.entries[0].panel == "unknown"


# ─── the camera switch ───────────────────────────────────────────────

def test_the_camera_can_be_switched_only_when_call_info_says_so(monkeypatch):
    rig = Rig(monkeypatch)
    monkeypatch.setattr(sip, "in_call", True)
    ok, _ = run(rig.hub.async_switch_camera(True))
    assert not ok and rig.sent == []
    run(rig.message('CALL_INFO;{"SIP_ID":"55001","MEDIA_TYPE":1,"VIDEO_SRC":1}'))
    assert rig.hub.camera_switch_available
    ok, _ = run(rig.hub.async_switch_camera(False))
    assert ok
    assert rig.sent == [("sip:60002@example.invalid",
                         'CALL_SWITCH_SOURCE;{"SOURCE_TYPE":"VINP"}',
                         {"Panda": "blue"})]
    monkeypatch.setattr(sip, "in_call", False)
    run(rig.hub._handle_broadcast("call_ended", "Call ended"))
    assert not rig.hub.camera_switch_available


# ─── video messages ──────────────────────────────────────────────────

def _mailbox(rows) -> str:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE MAILBOX (ID, IDMAILBOX, ORIGTIME, CALLERID, "
                "FILENAME, FLAG, DURATION, CALLTYPE)")
    con.executemany("INSERT INTO MAILBOX VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return base64.b64encode(con.serialize()).decode()


ROW1 = (1, 21, 1_700_000_000, 55001, 21001, "R", 10.0, "VIDEO")
ROW2 = (2, 21, 1_700_000_600, 55002, 21002, "", 5.0, "VIDEO")


def _deliver_mailbox(rig, rows):
    async def scenario():
        await rig.message(_mailbox(rows), panda="grey", koala="mailbox.db")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if rig.hub.video_messages is not None and len(
                    rig.hub.video_messages) == len(rows):
                return
    run(scenario())


def test_the_status_reply_is_followed_by_a_mailbox_request(monkeypatch):
    rig = Rig(monkeypatch)

    async def scenario():
        rig.hub._plant_fetcher = object()  # enable the sync
        monkeypatch.setattr(hub, "PLANT_STATUS_TIMEOUT", 0.2)

        async def _send(uri, body, extra_headers=None):
            rig.sent.append((uri, body, extra_headers))
            if body == "GET_INIT_STATUS":
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(rig.message(STATUS)))
            return True, "OK (200)"

        monkeypatch.setattr(sip, "do_system_message", _send)
        await rig.hub._sync_plant_once()
        await _settle()

    run(scenario())
    assert (PICG, "VM;GET_DB", {"Panda": "blue"}) in rig.sent


def test_the_mailbox_is_read_and_only_new_messages_are_announced(monkeypatch):
    rig = Rig(monkeypatch)
    _deliver_mailbox(rig, [ROW1])
    assert [m.id for m in rig.hub.video_messages] == ["1"]
    assert rig.events("vimar_intercom_video_message") == []
    _deliver_mailbox(rig, [ROW1, ROW2])
    (event,) = rig.events("vimar_intercom_video_message")
    assert event["id"] == "2" and event["caller_name"] == "Lobby"


def test_a_new_message_notification_refetches(monkeypatch):
    rig = Rig(monkeypatch)

    async def scenario():
        await rig.message("VM;VIDEO_MESSAGE_CHANGE;NEW;0")
        await _settle()

    run(scenario())
    assert rig.sent == [(PICG, "VM;GET_DB", {"Panda": "blue"})]


def test_a_mailbox_of_another_family_is_ignored(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message(_mailbox([ROW1]), panda="white", koala="mailbox.db"))
    assert rig.hub.video_messages is None


def test_mark_read_delete_and_delete_all_send_the_sdk_strings(monkeypatch):
    rig = Rig(monkeypatch)
    _deliver_mailbox(rig, [ROW1, ROW2])

    async def scenario():
        assert (await rig.hub.async_mark_video_message_read("2"))[0]
        assert (await rig.hub.async_delete_video_message("1"))[0]
        assert (await rig.hub.async_delete_all_video_messages())[0]
        await _settle()

    run(scenario())
    bodies = [body for _uri, body, _h in rig.sent]
    assert "VM;2;READ;1700000600;55002" in bodies
    assert "VM;1;DELETED;1700000000;55001" in bodies
    assert "VM;ALL;DELETED" in bodies
    assert bodies.count("VM;GET_DB") >= 1
    assert rig.hub.video_messages == ()


def test_playing_calls_the_prefixed_extension_and_marks_read(monkeypatch):
    rig = Rig(monkeypatch)
    _deliver_mailbox(rig, [ROW1, ROW2])

    async def scenario():
        ok, _ = await rig.hub.async_play_video_message("2")
        assert ok
        await _settle()

    run(scenario())
    assert rig.calls == ["call:sip:90002@example.invalid"]
    assert any(body.startswith("VM;2;READ;") for _u, body, _h in rig.sent)


def test_an_unknown_message_is_an_error_not_a_send(monkeypatch):
    rig = Rig(monkeypatch)
    ok, msg = run(rig.hub.async_play_video_message("1"))
    assert not ok and "not been read" in msg
    _deliver_mailbox(rig, [ROW1])
    ok, msg = run(rig.hub.async_delete_video_message("9"))
    assert not ok and "9" in msg
    assert all(body == "VM;GET_DB" for _u, body, _h in rig.sent)


def test_diagnostics_hold_no_identity(monkeypatch):
    rig = Rig(monkeypatch)
    run(rig.message(STATUS))
    text = repr(rig.hub.diagnostics())
    for secret in ("60901", "examplepassword", "example.invalid", "Front gate"):
        assert secret not in text
    assert "'dnd': True" in text
