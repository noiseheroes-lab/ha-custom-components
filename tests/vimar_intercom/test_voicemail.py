"""Tests for the video-message mailbox: the database the indoor unit sends,
the commands that mark and delete messages, and the playback extension.

The mailbox is built here with the columns the SDK reads
(`TABLE_MAILBOX`); every value in it is invented.
"""

import base64
import sqlite3

import pytest

from custom_components.vimar_intercom import voicemail as vm

COLUMNS = ("ID", "IDMAILBOX", "ORIGTIME", "CALLERID", "FILENAME", "FLAG",
           "DURATION", "CALLTYPE", "RECORDING")


def _mailbox(rows, columns=COLUMNS) -> bytes:
    con = sqlite3.connect(":memory:")
    con.execute(f"CREATE TABLE MAILBOX ({', '.join(columns)})")
    con.executemany(
        f"INSERT INTO MAILBOX VALUES ({', '.join('?' * len(columns))})", rows)
    con.commit()
    data = con.serialize()
    con.close()
    return data


ROWS = [
    (1, 21, 1_700_000_000, 55001, 21001, "R", 12.5, "VIDEO", 0),
    (2, 21, 1_700_000_600, 55002, 21002, "", None, "AUDIO", 0),
    (3, 21, 1_700_000_300, 55001, 21003, None, 4.0, "SOMETHING", 0),
]


def test_messages_come_newest_first_with_every_field():
    messages = vm.parse_mailbox(_mailbox(ROWS))
    assert [m.id for m in messages] == ["2", "3", "1"]
    newest = messages[0]
    assert newest.mailbox == 21
    assert newest.orig_time == 1_700_000_600
    assert newest.caller_id == "55002"
    assert newest.filename == 21002
    assert newest.read is False
    assert newest.duration is None
    assert newest.kind == "audio"
    oldest = messages[-1]
    assert oldest.read is True and oldest.duration == 12.5
    assert oldest.kind == "video"


def test_an_unknown_call_type_is_video_as_in_the_sdk():
    messages = {m.id: m for m in vm.parse_mailbox(_mailbox(ROWS))}
    assert messages["3"].kind == "video"


def test_optional_columns_may_be_missing():
    columns = ("ID", "IDMAILBOX", "ORIGTIME", "CALLERID", "FILENAME", "FLAG")
    data = _mailbox([(7, 21, 1_700_000_000, 55001, 21007, "R")], columns)
    (message,) = vm.parse_mailbox(data)
    assert message.duration is None
    assert message.kind == "video"


def test_the_body_is_base64_even_with_line_breaks():
    data = _mailbox(ROWS)
    encoded = base64.encodebytes(data).decode()
    assert "\n" in encoded
    assert vm.decode_mailbox(encoded) == data


@pytest.mark.parametrize("body", ["", "!!!not base64!!!", "aGVsbG8="])
def test_a_body_that_is_not_a_mailbox_is_rejected(body):
    with pytest.raises(ValueError):
        vm.parse_mailbox(vm.decode_mailbox(body))


def test_a_database_without_the_mailbox_table_is_rejected():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE OTHER (A)")
    data = con.serialize()
    with pytest.raises(ValueError):
        vm.parse_mailbox(data)


def test_an_oversized_mailbox_is_rejected():
    with pytest.raises(ValueError):
        vm.parse_mailbox(b"SQLite format 3\x00" + b"\x00" * vm.MAX_MAILBOX_BYTES)


def test_rows_that_cannot_be_addressed_are_skipped():
    rows = [
        (None, 21, 1, 55001, 21001, "", 1.0, "VIDEO", 0),
        ("1;x", 21, 1, 55001, 21001, "", 1.0, "VIDEO", 0),
        (5, 21, 1, "55;01", 21005, "", 1.0, "VIDEO", 0),
        (6, 21, 1, 55001, 21006, "", 1.0, "VIDEO", 0),
    ]
    assert [m.id for m in vm.parse_mailbox(_mailbox(rows))] == ["6"]


def test_read_and_delete_are_the_sdk_strings():
    (message, *_rest) = vm.parse_mailbox(_mailbox(ROWS[:1]))
    assert vm.read_command(message) == "VM;1;READ;1700000000;55001"
    assert vm.delete_command(message) == "VM;1;DELETED;1700000000;55001"
    assert vm.DELETE_ALL == "VM;ALL;DELETED"


@pytest.mark.parametrize(("prefix", "expected"), [
    ("90", "90001"),      # the mailbox number is replaced by the prefix
    (None, "21001"),      # no phonebook: the file name is dialled
    ("", "21001"),
    ("9x", "21001"),      # a prefix that is not an extension is ignored
])
def test_playback_dials_the_prefix_in_place_of_the_mailbox(prefix, expected):
    (message, *_rest) = vm.parse_mailbox(_mailbox(ROWS[:1]))
    assert vm.playback_extension(message, prefix) == expected


def test_a_file_name_not_starting_with_its_mailbox_is_dialled_as_is():
    rows = [(1, 21, 1, 55001, 99001, "", 1.0, "VIDEO", 0)]
    (message,) = vm.parse_mailbox(_mailbox(rows))
    assert vm.playback_extension(message, "90") == "99001"


def test_the_attribute_form_has_an_iso_time_and_the_panel_name():
    (message, *_rest) = vm.parse_mailbox(_mailbox(ROWS[:1]))
    attr = message.as_attribute({"55001": "Front gate"})
    assert attr == {
        "id": "1", "time": "2023-11-14T22:13:20+00:00", "caller_id": "55001",
        "caller_name": "Front gate", "duration": 12.5, "read": True,
        "type": "video",
    }
    assert message.as_attribute({})["caller_name"] == "55001"
