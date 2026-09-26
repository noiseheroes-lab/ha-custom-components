"""The indoor unit's system messages: what they say, parsed.

The indoor unit (the "PICG" in Vimar's SDK) talks to the apps over SIP
MESSAGE with a plain-text body. Requests go out with a `Panda` header
naming their family — `blue` for status and notifications, `command`
for a door or actuator — and the unit answers, or notifies on its own,
with a MESSAGE whose body holds one or more `;`-separated lines. None of
this is documented by Vimar; the shapes below are what the official
app's SDK parses (`SystemMsgModelReceiver.handleType`,
`MsgStatusReplyReceiver`, `MsgNewPhonebookReceiver`,
`SystemMessageReceiverVM`).

Nothing here imports Home Assistant or does I/O, so the whole of it is
unit tested directly. Every line is classified, and the lines the
integration acts on are parsed into dataclasses: the status reply, the
new-phonebook notification, do-not-disturb and voicemail switches, the
apartment-parameter replies, missed calls, call information and the
"answered elsewhere" notice. The requests the integration sends are
built here too, so the exact strings the SDK sends live in one place
and are tested against it.

Nothing in this module logs. The status reply carries the phonebook
download password (`token`), so a body must never be written to a log,
at any level; `summarize_body` gives the kinds of lines a body held,
which is what a debug line actually needs.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# What goes out.
GET_INIT_STATUS = "GET_INIT_STATUS"
PANDA_HEADER = "Panda"
PANDA_BLUE = "blue"
PANDA_COMMAND = "command"
# Apartment-parameter changes (the voicemail timeout) go out as `set`.
PANDA_SET = "set"
# The voicemail database comes back as `grey`, with `Koala: mailbox.db`.
PANDA_GREY = "grey"
KOALA_HEADER = "Koala"
KOALA_MAILBOX = "mailbox.db"

VM_GET_DB = "VM;GET_DB"
DND_PREFIX = "DND;"
VOICEMAIL_PREFIX = "VOICEMAIL;"

# The prefixes the SDK recognises, as (kind, marker, match) in the order
# `handleType` checks them. The order matters: the SDK tests
# `contains`, not `startswith`, and the first hit wins. `split` means
# the SDK's other test — every `;`-token of the marker appears among the
# line's `;`-tokens — which is how `C;<id>;ANSWERED` is recognised.
KIND_INIT_STATUS_REPLY = "init_status_reply"
KIND_NICKS_REPLY = "nicks_reply"
KIND_NICK_CHANGE = "nick_change"
KIND_NEW_VOICEMAIL = "new_voicemail"
KIND_VOICEMAIL_CHANGE = "voicemail_change"
KIND_APT_NAME_CHANGE = "apt_name_change"
KIND_FUORI_PORTA = "fuori_porta"
KIND_NEW_PHONEBOOK = "new_phonebook"
KIND_MISSED_CALL = "missed_call"
KIND_CALL_INFO = "call_info"
KIND_VOICEMAIL_STATUS = "voicemail_status"
KIND_DND_STATUS = "dnd_status"
KIND_SET_APT_PARAMS_REPLY = "set_apt_params_reply"
KIND_APT_PARAMS_CHANGED = "apt_params_changed"
KIND_CALL_ANSWERED = "call_answered"
KIND_CALL_READ = "call_read"
KIND_MSG_READ = "msg_read"
KIND_UNKNOWN = "unknown"

_INIT_STATUS_REPLY_PREFIX = "GET_INIT_STATUS_REPLY;"
_NEW_PHONEBOOK_PREFIX = "NEW_PHONEBOOK;"

_CONTAINS_MARKERS: tuple[tuple[str, str], ...] = (
    (KIND_INIT_STATUS_REPLY, _INIT_STATUS_REPLY_PREFIX),
    (KIND_NICKS_REPLY, "GET_NICKS_REPLY;"),
    (KIND_NICK_CHANGE, "NICK_CHANGE"),
    (KIND_NEW_VOICEMAIL, "VM;VIDEO_MESSAGE_CHANGE;NEW;"),
    (KIND_VOICEMAIL_CHANGE, "VM;VIDEO_MESSAGE_CHANGE;UPDATE"),
    (KIND_APT_NAME_CHANGE, "NAME_CHANGE;"),
    (KIND_FUORI_PORTA, "FP;"),
    (KIND_NEW_PHONEBOOK, _NEW_PHONEBOOK_PREFIX),
    (KIND_MISSED_CALL, "MISSED_CALL;"),
    (KIND_CALL_INFO, "CALL_INFO;"),
    (KIND_VOICEMAIL_STATUS, "VOICEMAIL;"),
    (KIND_DND_STATUS, "DND;"),
    (KIND_SET_APT_PARAMS_REPLY, "SET_APT_PARAMS_REPLY;"),
    (KIND_APT_PARAMS_CHANGED, "APT_PARAMS_CHANGED;"),
)
_TOKEN_MARKERS: tuple[tuple[str, frozenset[str]], ...] = (
    (KIND_CALL_ANSWERED, frozenset({"C", "ANSWERED"})),
    (KIND_CALL_READ, frozenset({"C", "READ"})),
    (KIND_MSG_READ, frozenset({"M", "READ"})),
)


def classify_line(line: str) -> str:
    """The kind of one system-message line, as the SDK would decide it."""
    for kind, marker in _CONTAINS_MARKERS:
        if marker in line:
            return kind
    tokens = set(line.split(";"))
    for kind, required in _TOKEN_MARKERS:
        if required <= tokens:
            return kind
    return KIND_UNKNOWN


def classify_body(body: str) -> Iterator[tuple[str, str]]:
    """Yield `(kind, line)` for every non-empty line of a MESSAGE body.

    One MESSAGE can carry several notifications, one per line — the
    unit batches `C;<id>;READ` updates that way. The SDK joins the
    non-empty lines back together and tests the whole; classifying each
    line on its own is equivalent for every single-line message and
    correct for a batched one.
    """
    for raw in body.splitlines():
        line = raw.strip()
        if line:
            yield classify_line(line), line


def summarize_body(body: str) -> str:
    """The kinds of lines a body held, safe to log."""
    kinds = [kind for kind, _line in classify_body(body)]
    return ",".join(kinds) if kinds else "empty"


@dataclass(frozen=True)
class InitStatus:
    """The indoor unit's answer to GET_INIT_STATUS.

    Phase 1 only uses `rubrica_ver`, `token` and `gid`, to download the
    phonebook. The rest is parsed now so that do-not-disturb and
    voicemail can read it later without touching the wire format again.

    `token` is the password of the phonebook download. It is excluded
    from `repr`, never persisted, and never logged.
    """

    gid: str | None = None
    apt_names: tuple[str, ...] | None = None
    dnd: bool | None = None
    voicemail: bool | None = None
    media_enc: str | None = None
    # The phonebook's version: the MD5 of the SQLite file the cloud
    # serves for it (the SDK checks the download against it).
    rubrica_ver: str | None = None
    token: str | None = field(default=None, repr=False)
    # "<messages stored>/<capacity>".
    vm_level: str | None = None
    vm_timeout: int | None = None
    vm_timeout_values: tuple[int, ...] | None = None
    vm_ver: str | None = None


def _first(params: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The first `{"PARAM": name, ...}` object, matched case-insensitively.

    The SDK matches with `equals(..., ignoreCase = true)` and stops at
    the first hit, so a duplicate later in the array is ignored.
    """
    wanted = name.lower()
    for param in params:
        if str(param.get("PARAM", "")).lower() == wanted:
            return param
    return None


def _text(params: list[dict[str, Any]], name: str) -> str | None:
    """A string parameter; absent or empty is None, as in the SDK."""
    param = _first(params, name)
    if param is None:
        return None
    value = param.get("VALUE")
    if value is None or isinstance(value, (list, dict)):
        return None
    text = str(value)
    return text or None


def _flag(params: list[dict[str, Any]], name: str) -> bool | None:
    """A "1"/"0" switch. The SDK treats anything but "1" as off."""
    param = _first(params, name)
    if param is None:
        return None
    return str(param.get("VALUE", "0")) == "1"


def _int(value: Any) -> int | None:
    """A non-negative integer, or None — the SDK maps negatives to null."""
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def parse_init_status_reply(body: str) -> InitStatus:
    """Parse a `GET_INIT_STATUS_REPLY;[{"PARAM":..,"VALUE":..},...]` line.

    Raises ValueError when the line is not a status reply or its payload
    is not a JSON array. The message never quotes the payload: it holds
    the token.
    """
    line = body.strip()
    if not line.startswith(_INIT_STATUS_REPLY_PREFIX):
        raise ValueError("not a GET_INIT_STATUS_REPLY")
    payload = line[len(_INIT_STATUS_REPLY_PREFIX):]
    if not (payload.startswith("[") and payload.endswith("]")):
        raise ValueError("the status reply carries no parameter array")
    try:
        decoded = json.loads(payload)
    except ValueError as err:
        raise ValueError("the status reply is not valid JSON") from err
    params = [item for item in decoded if isinstance(item, dict)]
    if decoded and not params:
        raise ValueError("the status reply holds no parameter objects")

    apt_names = None
    apt = _first(params, "apt_names")
    if apt is not None and isinstance(apt.get("VALUE"), list):
        apt_names = tuple(str(name) for name in apt["VALUE"] if name is not None)

    timeout_values = None
    tv = _first(params, "vm_timeout_values")
    if tv is not None and isinstance(tv.get("VALUE"), list):
        timeout_values = tuple(
            number for number in (_int(v) for v in tv["VALUE"])
            if number is not None)

    vm_timeout_param = _first(params, "vm_timeout")
    return InitStatus(
        gid=_text(params, "GID"),
        apt_names=apt_names,
        dnd=_flag(params, "dnd"),
        voicemail=_flag(params, "voicemail"),
        media_enc=_text(params, "media_enc"),
        rubrica_ver=_text(params, "rubrica_ver"),
        token=_text(params, "token"),
        vm_level=_text(params, "vm_level"),
        vm_timeout=(_int(vm_timeout_param.get("VALUE"))
                    if vm_timeout_param is not None else None),
        vm_timeout_values=timeout_values,
        vm_ver=_text(params, "vm_ver"),
    )


@dataclass(frozen=True)
class NewPhonebook:
    """`NEW_PHONEBOOK;<version>;<gid>`: the installer changed the plant."""

    version: str
    gid: str | None


def parse_new_phonebook(body: str) -> NewPhonebook:
    """Parse a new-phonebook notification. Raises ValueError without a version."""
    line = body.strip()
    if not line.startswith(_NEW_PHONEBOOK_PREFIX):
        raise ValueError("not a NEW_PHONEBOOK notification")
    parts = line[len(_NEW_PHONEBOOK_PREFIX):].split(";")
    version = parts[0].strip()
    if not version:
        raise ValueError("the NEW_PHONEBOOK notification names no version")
    gid = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return NewPhonebook(version=version, gid=gid)


# ─── do-not-disturb and voicemail ────────────────────────────────────

def parse_switch(line: str, prefix: str) -> bool:
    """`DND;ON` / `VOICEMAIL;OFF`: on only for a case-insensitive "ON".

    The SDK removes every occurrence of the prefix and compares what is
    left with "ON", ignoring case (`MsgDndStatusReceiver`,
    `MsgVoicemailStatusReceiver`); anything else is off.
    """
    return line.replace(prefix, "").strip().upper() == "ON"


def dnd_command(on: bool) -> str:
    """The body that turns do-not-disturb on or off (to the SGA)."""
    return f"{DND_PREFIX}{'ON' if on else 'OFF'}"


def voicemail_command(on: bool) -> str:
    """The body that turns the answering machine on or off (to the SGA)."""
    return f"{VOICEMAIL_PREFIX}{'ON' if on else 'OFF'}"


_MSG_ID_ALPHABET = string.ascii_letters + string.digits


def new_message_id() -> str:
    """A fresh SET_APT_PARAMS message ID: eight letters and digits.

    The SDK draws the same alphabet (`generateRandomString`, default
    length 8). The reply echoes it, which is how a reply is matched to
    its request when two are in flight.
    """
    return "".join(secrets.choice(_MSG_ID_ALPHABET) for _ in range(8))


def _compact(obj: Any) -> str:
    """JSON the way Android's JSONObject.toString writes it: no spaces."""
    return json.dumps(obj, separators=(",", ":"))


def set_vm_timeout_command(msg_id: str, seconds: int) -> str:
    """`SET_APT_PARAMS;{"MSGID":..,"PARAM":"vm_timeout","VALUE":<int>}`.

    Sent with `Panda: set` to the indoor unit. The keys are in the order
    the SDK puts them in; the unit parses JSON, so the order should not
    matter, but there is no reason to find out.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise ValueError("the voicemail timeout must be a non-negative integer")
    if not msg_id.isalnum():
        raise ValueError("a message ID is letters and digits only")
    return "SET_APT_PARAMS;" + _compact(
        {"MSGID": msg_id, "PARAM": "vm_timeout", "VALUE": seconds})


def _json_after(line: str, marker: str) -> dict[str, Any]:
    """The JSON object after a line's marker, or {} when there is none.

    The SDK removes the marker and parses the rest, falling back to an
    empty object when that fails; a malformed line then reads as a line
    with no fields rather than as an error.
    """
    try:
        decoded = json.loads(line.replace(marker, "", 1).strip())
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _opt_text(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _opt_int(obj: dict[str, Any], key: str) -> int | None:
    """A non-negative int field, as the SDK's `optInt(key, -1)` reads it."""
    return _int(obj.get(key))


@dataclass(frozen=True)
class AptParamsReply:
    """`SET_APT_PARAMS_REPLY;{"MSGID","ERRCODE"}`: how a change went."""

    msg_id: str | None
    error_code: str | None

    @property
    def ok(self) -> bool:
        """True for ERR_NONE, compared ignoring case as the SDK does."""
        return (self.error_code or "").upper() == "ERR_NONE"


def parse_apt_params_reply(line: str) -> AptParamsReply:
    """Parse a SET_APT_PARAMS_REPLY line; never raises."""
    obj = _json_after(line, "SET_APT_PARAMS_REPLY;")
    return AptParamsReply(msg_id=_opt_text(obj, "MSGID"),
                          error_code=_opt_text(obj, "ERRCODE"))


@dataclass(frozen=True)
class AptParamsChanged:
    """`APT_PARAMS_CHANGED;{"PARAM","VALUE"}`: someone changed a setting."""

    param: str | None
    vm_timeout: int | None


def parse_apt_params_changed(line: str) -> AptParamsChanged:
    """Parse an APT_PARAMS_CHANGED line. Only vm_timeout is read, as in the SDK."""
    obj = _json_after(line, "APT_PARAMS_CHANGED;")
    param = _opt_text(obj, "PARAM")
    timeout = (_opt_int(obj, "VALUE")
               if param is not None and param.lower() == "vm_timeout" else None)
    return AptParamsChanged(param=param, vm_timeout=timeout)


_VM_LEVEL_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def parse_vm_level(raw: str | None) -> tuple[int, int] | None:
    """`vm_level` — "<messages stored>/<capacity>" — as two ints, or None."""
    if raw is None:
        return None
    match = _VM_LEVEL_RE.match(raw)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


# ─── calls ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MissedCall:
    """`MISSED_CALL;{"SIP_ID","TS"}`: a panel rang and nobody answered.

    `timestamp` is the number the unit sent, unconverted: the SDK only
    stores it, and whether it counts seconds or milliseconds is not
    visible from the app (see `call_log.epoch_seconds`).
    """

    sip_id: str | None
    timestamp: int | None


def parse_missed_call(line: str) -> MissedCall:
    """Parse a MISSED_CALL line; never raises."""
    obj = _json_after(line, "MISSED_CALL;")
    sip_id = _opt_text(obj, "SIP_ID")
    ts = _opt_text(obj, "TS")
    timestamp = int(ts) if ts is not None and ts.isdigit() else None
    return MissedCall(sip_id=sip_id, timestamp=timestamp)


@dataclass(frozen=True)
class CallInfo:
    """`CALL_INFO;{"SIP_ID","REASON","MEDIA_TYPE","VIDEO_SRC"}`.

    `VIDEO_SRC` 1 means the calling panel has more than one camera and
    CALL_SWITCH_SOURCE can step through them (`isSwitchVideoAvailable`).
    """

    sip_id: str | None
    reason: int | None
    media_type: int | None
    video_source: int | None

    @property
    def switch_available(self) -> bool:
        """True when the camera of this call can be switched."""
        return self.video_source == 1

    @property
    def is_video(self) -> bool:
        """MEDIA_TYPE 1 or 2 is a video call, 0 an audio-only one."""
        return self.media_type in (1, 2)


def parse_call_info(line: str) -> CallInfo:
    """Parse a CALL_INFO line; never raises."""
    obj = _json_after(line, "CALL_INFO;")
    return CallInfo(sip_id=_opt_text(obj, "SIP_ID"),
                    reason=_opt_int(obj, "REASON"),
                    media_type=_opt_int(obj, "MEDIA_TYPE"),
                    video_source=_opt_int(obj, "VIDEO_SRC"))


def parse_call_answered(line: str) -> str | None:
    """The call ID of a `C;<call id>;ANSWERED` line, or None.

    The SDK takes the second `;`-field of each line
    (`MsgCallAnsweredReceiver`).
    """
    parts = line.split(";")
    if len(parts) < 2:
        return None
    return parts[1].strip() or None


# Printable ASCII without spaces or `;`. A call ID comes off the wire, in
# an INVITE header, and goes back out inside a MESSAGE body whose lines
# are split on newlines and fields on `;`: either would forge a field or
# a whole extra notification.
_CALL_ID_RE = re.compile(r"^[!-:<-~]{1,128}$")


def valid_call_id(call_id: str) -> bool:
    """True when a call ID may be embedded in a system-message body."""
    return bool(_CALL_ID_RE.match(call_id))


def call_answered_command(call_id: str) -> str:
    """`C;<call id>;ANSWERED`: tell the other devices this one answered."""
    if not valid_call_id(call_id):
        raise ValueError("that call ID cannot be sent in a system message")
    return f"C;{call_id};ANSWERED"


def switch_source_command(forward: bool) -> str:
    """`CALL_SWITCH_SOURCE;{"SOURCE_TYPE":"VINN"|"VINP"}`: next/previous camera."""
    return "CALL_SWITCH_SOURCE;" + _compact(
        {"SOURCE_TYPE": "VINN" if forward else "VINP"})


def mailbox_full(line: str) -> bool:
    """`VM;VIDEO_MESSAGE_CHANGE;NEW;1` says the mailbox is now full."""
    return line.replace("VM;VIDEO_MESSAGE_CHANGE;NEW;", "").strip() == "1"
