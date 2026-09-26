"""Tests for the indoor unit's system-message parsing.

The fixtures follow the shape the official app parses (see
`system_messages.py`); every value in them is invented.
"""

import json

import pytest

from custom_components.vimar_intercom import system_messages as sm


def _reply(params: list[dict]) -> str:
    return "GET_INIT_STATUS_REPLY;" + json.dumps(params)


FULL_REPLY = _reply([
    {"PARAM": "GID", "VALUE": "21"},
    {"PARAM": "apt_names", "VALUE": ["Flat 1", "", "Extra"]},
    {"PARAM": "dnd", "VALUE": "0"},
    {"PARAM": "media_enc", "VALUE": "SRTP"},
    {"PARAM": "rubrica_ver", "VALUE": "0123456789abcdef0123456789abcdef"},
    {"PARAM": "token", "VALUE": "not-a-real-token"},
    {"PARAM": "vm_level", "VALUE": "3/20"},
    {"PARAM": "vm_timeout", "VALUE": 30},
    {"PARAM": "vm_timeout_values", "VALUE": [15, 30, 60, -1]},
    {"PARAM": "vm_ver", "VALUE": "fedcba9876543210fedcba9876543210"},
    {"PARAM": "voicemail", "VALUE": "1"},
])


# ─── GET_INIT_STATUS_REPLY ───────────────────────────────────────────

def test_a_full_reply_parses_every_field():
    status = sm.parse_init_status_reply(FULL_REPLY)

    assert status.gid == "21"
    assert status.apt_names == ("Flat 1", "", "Extra")
    assert status.dnd is False
    assert status.voicemail is True
    assert status.media_enc == "SRTP"
    assert status.rubrica_ver == "0123456789abcdef0123456789abcdef"
    assert status.token == "not-a-real-token"
    assert status.vm_level == "3/20"
    assert status.vm_timeout == 30
    assert status.vm_timeout_values == (15, 30, 60)
    assert status.vm_ver == "fedcba9876543210fedcba9876543210"


def test_the_token_never_appears_in_the_repr():
    """A status object that ends up in a log line must not carry the
    phonebook password along with it."""
    status = sm.parse_init_status_reply(FULL_REPLY)
    assert "not-a-real-token" not in repr(status)


def test_parameter_names_are_matched_case_insensitively():
    status = sm.parse_init_status_reply(_reply([
        {"PARAM": "RUBRICA_VER", "VALUE": "abc"},
        {"PARAM": "Token", "VALUE": "t"},
        {"PARAM": "gid", "VALUE": "22"},
    ]))
    assert (status.rubrica_ver, status.token, status.gid) == ("abc", "t", "22")


def test_missing_and_empty_fields_are_none():
    status = sm.parse_init_status_reply(_reply([
        {"PARAM": "rubrica_ver", "VALUE": ""},
        {"PARAM": "dnd", "VALUE": "1"},
    ]))
    assert status.rubrica_ver is None
    assert status.token is None
    assert status.gid is None
    assert status.dnd is True
    assert status.voicemail is None
    assert status.vm_timeout is None
    assert status.vm_timeout_values is None
    assert status.apt_names is None


def test_a_negative_voicemail_timeout_means_none():
    status = sm.parse_init_status_reply(_reply([
        {"PARAM": "vm_timeout", "VALUE": -1}]))
    assert status.vm_timeout is None


def test_the_first_occurrence_of_a_parameter_wins():
    status = sm.parse_init_status_reply(_reply([
        {"PARAM": "gid", "VALUE": "21"},
        {"PARAM": "gid", "VALUE": "99"},
    ]))
    assert status.gid == "21"


@pytest.mark.parametrize("body", [
    "GET_INIT_STATUS_REPLY;",
    "GET_INIT_STATUS_REPLY;{}",
    "GET_INIT_STATUS_REPLY;[not json]",
    "GET_INIT_STATUS_REPLY;[1, 2]",
    "NEW_PHONEBOOK;abc;21",
    "",
])
def test_a_malformed_reply_is_rejected(body):
    with pytest.raises(ValueError):
        sm.parse_init_status_reply(body)


def test_non_object_entries_are_skipped_not_fatal():
    """The app iterates JSON objects; anything else in the array is
    noise from a newer firmware, and must not cost the whole reply."""
    status = sm.parse_init_status_reply(
        'GET_INIT_STATUS_REPLY;[{"PARAM":"gid","VALUE":"21"}, "x", null]')
    assert status.gid == "21"


# ─── NEW_PHONEBOOK ───────────────────────────────────────────────────

def test_new_phonebook_carries_version_and_gid():
    note = sm.parse_new_phonebook("NEW_PHONEBOOK;0123abcd;21")
    assert note == sm.NewPhonebook(version="0123abcd", gid="21")


def test_new_phonebook_without_gid():
    assert sm.parse_new_phonebook("NEW_PHONEBOOK;0123abcd") == (
        sm.NewPhonebook(version="0123abcd", gid=None))


@pytest.mark.parametrize("body", ["NEW_PHONEBOOK;", "NEW_PHONEBOOK", "DND;ON"])
def test_new_phonebook_without_a_version_is_rejected(body):
    with pytest.raises(ValueError):
        sm.parse_new_phonebook(body)


# ─── line dispatch ───────────────────────────────────────────────────

def test_a_body_is_split_into_classified_lines():
    body = ("\r\n" + FULL_REPLY + "\nNEW_PHONEBOOK;abc;21\n\nDND;ON\n"
            "C;123;ANSWERED\nsomething else\n")
    kinds = [kind for kind, _line in sm.classify_body(body)]
    assert kinds == [
        sm.KIND_INIT_STATUS_REPLY,
        sm.KIND_NEW_PHONEBOOK,
        sm.KIND_DND_STATUS,
        sm.KIND_CALL_ANSWERED,
        sm.KIND_UNKNOWN,
    ]


def test_classification_follows_the_apps_order():
    """The app tests `contains` in a fixed order and takes the first hit,
    then falls back to matching `;`-tokens for the C;/M; lines. A
    different order would classify some lines differently."""
    assert sm.classify_line("GET_NICKS_REPLY;[]") == sm.KIND_NICKS_REPLY
    assert sm.classify_line("NAME_CHANGE;a;b;c") == sm.KIND_APT_NAME_CHANGE
    assert sm.classify_line("VM;VIDEO_MESSAGE_CHANGE;NEW;x") == (
        sm.KIND_NEW_VOICEMAIL)
    assert sm.classify_line("C;1;2;READ") == sm.KIND_CALL_READ
    assert sm.classify_line("M;9;READ") == sm.KIND_MSG_READ


def test_summary_names_the_kinds_and_never_the_content():
    summary = sm.summarize_body(FULL_REPLY + "\nhello there")
    assert "not-a-real-token" not in summary
    assert "hello" not in summary
    assert sm.KIND_INIT_STATUS_REPLY in summary


# ─── do-not-disturb, voicemail, apartment parameters ─────────────────

@pytest.mark.parametrize(("line", "expected"), [
    ("DND;ON", True), ("DND;OFF", False), ("DND;on", True), ("DND; ON ", True),
    ("DND;", False), ("DND;1", False),
])
def test_dnd_is_on_only_for_on(line, expected):
    assert sm.parse_switch(line, sm.DND_PREFIX) is expected


@pytest.mark.parametrize(("line", "expected"), [
    ("VOICEMAIL;ON", True), ("VOICEMAIL;OFF", False),
])
def test_voicemail_status(line, expected):
    assert sm.classify_line(line) == sm.KIND_VOICEMAIL_STATUS
    assert sm.parse_switch(line, sm.VOICEMAIL_PREFIX) is expected


def test_switch_commands_are_the_sdk_strings():
    assert sm.dnd_command(True) == "DND;ON"
    assert sm.dnd_command(False) == "DND;OFF"
    assert sm.voicemail_command(True) == "VOICEMAIL;ON"
    assert sm.voicemail_command(False) == "VOICEMAIL;OFF"


def test_set_vm_timeout_is_compact_json_in_the_sdk_key_order():
    body = sm.set_vm_timeout_command("aB3dE5gH", 60)
    assert body == ('SET_APT_PARAMS;{"MSGID":"aB3dE5gH","PARAM":"vm_timeout",'
                    '"VALUE":60}')


@pytest.mark.parametrize("seconds", [-1, True, "30"])
def test_set_vm_timeout_refuses_anything_but_a_non_negative_int(seconds):
    with pytest.raises(ValueError):
        sm.set_vm_timeout_command("aB3dE5gH", seconds)


def test_message_ids_are_eight_alphanumerics_and_differ():
    first, second = sm.new_message_id(), sm.new_message_id()
    assert len(first) == 8 and first.isalnum() and first.isascii()
    assert first != second


def test_apt_params_reply_success_and_failure():
    ok = sm.parse_apt_params_reply(
        'SET_APT_PARAMS_REPLY;{"MSGID":"abc","ERRCODE":"ERR_NONE"}')
    assert (ok.msg_id, ok.ok) == ("abc", True)
    bad = sm.parse_apt_params_reply(
        'SET_APT_PARAMS_REPLY;{"MSGID":"abc","ERRCODE":"ERR_INVALID_VALUE"}')
    assert (bad.ok, bad.error_code) == (False, "ERR_INVALID_VALUE")


def test_an_unreadable_apt_params_reply_is_a_failure_without_an_id():
    reply = sm.parse_apt_params_reply("SET_APT_PARAMS_REPLY;not json")
    assert reply.msg_id is None
    assert reply.ok is False


def test_apt_params_changed_names_the_timeout():
    change = sm.parse_apt_params_changed(
        'APT_PARAMS_CHANGED;{"PARAM":"vm_timeout","VALUE":45}')
    assert change.vm_timeout == 45
    other = sm.parse_apt_params_changed(
        'APT_PARAMS_CHANGED;{"PARAM":"apt_names","VALUE":["a"]}')
    assert other.vm_timeout is None


@pytest.mark.parametrize(("raw", "expected"), [
    ("3/20", (3, 20)), (" 0 / 10 ", (0, 10)), ("7/0", (7, 0)),
    ("x/10", None), ("3", None), (None, None), ("-1/10", None),
])
def test_vm_level_is_used_over_capacity(raw, expected):
    assert sm.parse_vm_level(raw) == expected


# ─── calls ───────────────────────────────────────────────────────────

def test_missed_call_carries_the_panel_and_its_timestamp():
    missed = sm.parse_missed_call('MISSED_CALL;{"SIP_ID":"55001","TS":"1700000000"}')
    assert missed.sip_id == "55001"
    assert missed.timestamp == 1700000000


def test_a_numeric_missed_call_timestamp_is_accepted():
    missed = sm.parse_missed_call('MISSED_CALL;{"SIP_ID":"55001","TS":1700000000}')
    assert missed.timestamp == 1700000000


@pytest.mark.parametrize("line", [
    "MISSED_CALL;{}", "MISSED_CALL;not json", 'MISSED_CALL;{"TS":"x"}',
])
def test_a_missed_call_without_usable_fields_has_none(line):
    missed = sm.parse_missed_call(line)
    assert missed.timestamp is None
    assert missed.sip_id in (None,)


def test_call_info_says_when_the_camera_can_be_switched():
    info = sm.parse_call_info(
        'CALL_INFO;{"SIP_ID":"55001","REASON":0,"MEDIA_TYPE":1,"VIDEO_SRC":1}')
    assert info.sip_id == "55001"
    assert info.switch_available is True
    assert info.is_video is True
    assert sm.parse_call_info('CALL_INFO;{"VIDEO_SRC":0}').switch_available is False
    assert sm.parse_call_info("CALL_INFO;{}").switch_available is False


def test_call_answered_lines_name_their_call():
    assert sm.parse_call_answered("C;abc123@host;ANSWERED") == "abc123@host"
    assert sm.parse_call_answered("C;;ANSWERED") is None


def test_the_answered_notification_is_the_sdk_string():
    assert sm.call_answered_command("abc@host") == "C;abc@host;ANSWERED"


@pytest.mark.parametrize("call_id", ["", "a;b", "a\nb", "a b", "x" * 200])
def test_an_unsafe_call_id_never_reaches_a_body(call_id):
    with pytest.raises(ValueError):
        sm.call_answered_command(call_id)


def test_camera_switch_commands():
    assert sm.switch_source_command(True) == 'CALL_SWITCH_SOURCE;{"SOURCE_TYPE":"VINN"}'
    assert sm.switch_source_command(False) == 'CALL_SWITCH_SOURCE;{"SOURCE_TYPE":"VINP"}'


def test_new_video_message_says_whether_the_mailbox_is_full():
    assert sm.classify_line("VM;VIDEO_MESSAGE_CHANGE;NEW;1") == sm.KIND_NEW_VOICEMAIL
    assert sm.mailbox_full("VM;VIDEO_MESSAGE_CHANGE;NEW;1") is True
    assert sm.mailbox_full("VM;VIDEO_MESSAGE_CHANGE;NEW;0") is False
    assert sm.classify_line("VM;VIDEO_MESSAGE_CHANGE;UPDATE") == (
        sm.KIND_VOICEMAIL_CHANGE)
