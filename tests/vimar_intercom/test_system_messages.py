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
