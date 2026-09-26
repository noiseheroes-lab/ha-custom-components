"""The indoor unit's video-message mailbox (its answering machine).

When the answering machine is on, a visitor nobody answers can leave a
video message, and the indoor unit keeps it. The official app lists
them by asking the unit for its mailbox database: a `VM;GET_DB`
MESSAGE (`Panda: blue`) to the indoor unit, which answers with a MESSAGE
of its own, `Panda: grey` and `Koala: mailbox.db`, whose body is the
whole SQLite file in base64 (`VoicemailDbManager`). The app reads the
`MAILBOX` table newest first (`QUERY_GET_MAILBOX`) and turns each row
into a `VideoMsgModel`; this module does the same.

A message is marked read or deleted with a one-line MESSAGE to the
indoor unit (`VM;<ID>;READ;<ORIGTIME>;<CALLERID>`,
`VM;<ID>;DELETED;...`, `VM;ALL;DELETED`). There is no reply; the unit
announces the change with `VM;VIDEO_MESSAGE_CHANGE;UPDATE`, and the
mailbox is fetched again.

Playing a message is not a download: there is no media URL. The app
places an ordinary SIP call to an extension built from the message's
file name, with the mailbox number at its start replaced by the plant's
`VM_PREFIX` (`VideoMsgModel.remoteUsernameWithPrefix`), and the unit
plays the recording as the media of that call.

Nothing here imports Home Assistant or does network I/O. Parsing is
blocking (sqlite3) and runs in the executor.
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .call_log import epoch_seconds, iso_time
from .plant_config import open_sqlite_bytes, table_rows
from .runtime import valid_sip_token

# A mailbox holds a few dozen rows and fits in a handful of SQLite
# pages. The ceiling only stops a misbehaving peer from making the
# integration decode and hold an arbitrarily large blob.
MAX_MAILBOX_BYTES = 2 * 1024 * 1024
_SQLITE_MAGIC = b"SQLite format 3\x00"

DELETE_ALL = "VM;ALL;DELETED"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"


@dataclass(frozen=True)
class VideoMessage:
    """One recorded message, as the SDK's `VideoMsgModel` holds it."""

    id: str
    mailbox: int
    orig_time: int
    caller_id: str
    filename: int
    read: bool
    duration: float | None
    kind: str

    @property
    def time(self) -> float | None:
        """When it was recorded, in epoch seconds, if the unit said."""
        return epoch_seconds(self.orig_time)

    def as_attribute(self, panel_names: Mapping[str, str]) -> dict[str, Any]:
        """What the video-message sensor shows for this message."""
        when = self.time
        return {
            "id": self.id,
            "time": iso_time(when) if when is not None else None,
            "caller_id": self.caller_id,
            "caller_name": panel_names.get(self.caller_id, self.caller_id),
            "duration": self.duration,
            "read": self.read,
            "type": self.kind,
        }


def decode_mailbox(body: str) -> bytes:
    """The database bytes of a `Koala: mailbox.db` MESSAGE body.

    Android's `Base64.decode(text, DEFAULT)` skips line breaks, so the
    body may be wrapped; everything but the base64 alphabet is dropped
    before decoding, the same way.
    """
    compact = "".join(body.split())
    if len(compact) > MAX_MAILBOX_BYTES * 4 // 3 + 4:
        raise ValueError("the mailbox is larger than any real one")
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("the mailbox body is not base64") from err


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def _duration(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _message(row: dict[str, Any]) -> VideoMessage | None:
    """One MAILBOX row, or None when it cannot be addressed safely.

    The ID, time and caller go back out inside a `VM;...` body, split on
    `;`, so each must be a plain number; a row that is not is skipped
    rather than repaired.
    """
    msg_id, mailbox = _int(row.get("ID")), _int(row.get("IDMAILBOX"))
    orig_time, caller = _int(row.get("ORIGTIME")), _int(row.get("CALLERID"))
    filename = _int(row.get("FILENAME"))
    if None in (msg_id, mailbox, orig_time, caller, filename):
        return None
    call_type = row.get("CALLTYPE")
    return VideoMessage(
        id=str(msg_id),
        mailbox=mailbox,
        orig_time=orig_time,
        caller_id=str(caller),
        filename=filename,
        # The SDK: read exactly when FLAG is "R".
        read=row.get("FLAG") == "R",
        duration=_duration(row.get("DURATION")),
        # The SDK maps "AUDIO" to audio and everything else to video.
        kind=KIND_AUDIO if call_type == "AUDIO" else KIND_VIDEO,
    )


def parse_mailbox(data: bytes) -> tuple[VideoMessage, ...]:
    """The messages of a mailbox database, newest first.

    Raises ValueError when the bytes are not a mailbox.
    """
    if len(data) > MAX_MAILBOX_BYTES:
        raise ValueError("the mailbox is larger than any real one")
    if not data.startswith(_SQLITE_MAGIC):
        raise ValueError("the mailbox is not an SQLite database")
    try:
        con = open_sqlite_bytes(data)
    except sqlite3.Error as err:
        raise ValueError("the mailbox database cannot be opened") from err
    try:
        rows = table_rows(con, "MAILBOX")
    except sqlite3.Error as err:
        raise ValueError("the mailbox database is unreadable") from err
    finally:
        con.close()
    if rows is None:
        raise ValueError("the database has no MAILBOX table")
    messages = [m for m in (_message(row) for row in rows) if m is not None]
    messages.sort(key=lambda m: m.orig_time, reverse=True)
    return tuple(messages)


def read_command(message: VideoMessage) -> str:
    """`VM;<ID>;READ;<ORIGTIME>;<CALLERID>`, to the indoor unit."""
    return f"VM;{message.id};READ;{message.orig_time};{message.caller_id}"


def delete_command(message: VideoMessage) -> str:
    """`VM;<ID>;DELETED;<ORIGTIME>;<CALLERID>`, to the indoor unit."""
    return f"VM;{message.id};DELETED;{message.orig_time};{message.caller_id}"


def playback_extension(message: VideoMessage, prefix: str | None) -> str:
    """The extension a playback call dials.

    The file name with its leading mailbox number replaced by the
    plant's VM_PREFIX, as `remoteUsernameWithPrefix` does it; without a
    usable prefix (no phonebook), the file name itself, which is what
    the SDK dials then.
    """
    filename = str(message.filename)
    mailbox = str(message.mailbox)
    if (prefix and valid_sip_token(prefix) and prefix.isdigit()
            and filename.startswith(mailbox)):
        return prefix + filename[len(mailbox):]
    return filename
