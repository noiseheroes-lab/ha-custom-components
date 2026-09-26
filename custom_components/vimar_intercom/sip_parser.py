"""Pure SIP text handling: parsing, transaction keys, expiry.

No sockets, no Home Assistant, no logging — everything here is a
function of its arguments so it can be unit tested directly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# A parameter value ends at a comma, a semicolon, or the closing angle
# bracket of a URI, unless it is quoted.
_PARAM_RE = re.compile(r'([\w.+-]+)\s*=\s*("([^"]*)"|[^,;>]*)')


@dataclass(frozen=True)
class ParsedMessage:
    """A SIP message split into its parts."""

    code: int | None
    method: str | None
    headers: dict[str, str]
    via_list: list[str] = field(default_factory=list)
    body: str = ""
    start_line: str = ""


def parse_message(raw: str) -> ParsedMessage:
    """Split a raw SIP message into start line, headers and body.

    Repeated headers keep the last value, except Via, where the full
    ordered list is preserved in `via_list`.
    """
    head, _, body = raw.partition("\r\n\r\n")
    lines = head.split("\r\n")
    start_line = lines[0] if lines else ""

    code: int | None = None
    method: str | None = None
    if start_line.startswith("SIP/2.0"):
        parts = start_line.split()
        if len(parts) > 1 and parts[1].isdigit():
            code = int(parts[1])
    elif start_line:
        method = start_line.split()[0]

    headers: dict[str, str] = {}
    via_list: list[str] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.strip().lower()
        value = value.strip()
        if key == "via":
            # The topmost Via is ours; keep it, not the last one seen.
            via_list.append(value)
            headers.setdefault(key, value)
            continue
        headers[key] = value

    return ParsedMessage(
        code=code,
        method=method,
        headers=headers,
        via_list=via_list,
        body=body,
        start_line=start_line,
    )


def header_params(value: str) -> dict[str, str]:
    """Parse `name=value` parameters out of a header value."""
    params: dict[str, str] = {}
    for match in _PARAM_RE.finditer(value):
        name = match.group(1).lower()
        params[name] = (match.group(3)
                        if match.group(3) is not None
                        else match.group(2).strip())
    return params


def via_branch(headers: Mapping[str, str]) -> str:
    """Return the branch parameter of the topmost Via, or ''."""
    return header_params(headers.get("via", "")).get("branch", "")


def cseq_parts(headers: Mapping[str, str]) -> tuple[int | None, str]:
    """Split the CSeq header into (sequence, method)."""
    parts = headers.get("cseq", "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None, ""
    return int(parts[0]), parts[1].upper()


def transaction_key(branch: str, seq: int | None, method: str) -> str:
    """Build the key that correlates a request with its responses."""
    return f"{branch}|{seq}|{method}"


def response_keys(msg: ParsedMessage) -> list[str]:
    """Every key this response could be waiting under, best first.

    A response carries the branch and CSeq of the request it answers, so
    that pair identifies the transaction even when several transactions
    share a Call-ID. The Call-ID key stays as a fallback for peers that
    do not echo the branch. A response with no parseable CSeq yields no
    keys at all: it is unroutable, and being unroutable is correct —
    misrouting it is the bug.
    """
    keys: list[str] = []
    branch = via_branch(msg.headers)
    seq, method = cseq_parts(msg.headers)
    if branch and seq is not None:
        keys.append(transaction_key(branch, seq, method))
    call_id = msg.headers.get("call-id", "")
    if call_id and seq is not None:
        keys.append(call_id_key(call_id, seq, method))
    return keys


def call_id_key(call_id: str, seq: int | None, method: str) -> str:
    """Fallback key for a peer that does not echo our branch.

    The CSeq is part of the key on purpose. A REGISTER and its
    authenticated retry share a Call-ID, so a bare `cid:` key would let a
    late or duplicated response from the first transaction be delivered
    to the second and accepted as its final response — discarding the
    real one. Including the CSeq makes the two keys distinct.
    """
    return f"cid:{call_id}|{seq}|{method}"


def addr_uri(header_value: str) -> str:
    """Return the URI carried by a From/To-style address header.

    SIP allows two forms: `name-addr` — an optional display name
    followed by the URI in angle brackets, e.g.
    `"Front Door" <sip:55001@example.com>` — and a bare `addr-spec` with
    no brackets at all, e.g. `sip:55001@example.com;tag=abc`, which is
    equally legal. The bracket form is preferred; when there are none,
    the text before the first `;` is the addr-spec, stripped of
    surrounding whitespace. A header with no address before either a
    `;` or the end of the string (including an empty header) yields ''.
    """
    if "<" in header_value and ">" in header_value:
        return header_value[header_value.index("<") + 1:header_value.index(">")]
    return header_value.split(";", 1)[0].strip()


def tag_of(header_value: str) -> str:
    """Return the `tag` parameter of a From/To header, or ''."""
    for part in header_value.split(";")[1:]:
        part = part.strip()
        if part.startswith("tag="):
            return part[4:].strip()
    return ""


def granted_expiry(msg: ParsedMessage, contact_user: str, requested: int) -> int:
    """Return the registration lifetime the registrar granted, in seconds.

    Prefers the `expires` parameter on our own Contact, then the Expires
    header, then the value we asked for.
    """
    contact = msg.headers.get("contact", "")
    if f"sip:{contact_user}@" in contact:
        expires = header_params(contact).get("expires")
        if expires and expires.isdigit():
            return int(expires)

    header = msg.headers.get("expires", "")
    if header.isdigit():
        return int(header)

    return requested


def header_values(raw: str, *names: str) -> list[str]:
    """Every value of the named headers, in the order they appear.

    `parse_message` keeps one value per header name. A few questions need
    all of them — an INVITE whose `Call-ID` appears twice, once from the
    SIP stack and once as the Vimar SDK's own call identifier — so this
    re-reads the header block. Names are matched case-insensitively.
    """
    wanted = {name.lower() for name in names}
    head = raw.partition("\r\n\r\n")[0]
    values: list[str] = []
    for line in head.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() in wanted and value.strip():
            values.append(value.strip())
    return values


def reason_cause(value: str) -> int | None:
    """The `cause` of the SIP entry of a Reason header (RFC 3326), or None.

    A CANCEL carrying `Reason: SIP;cause=200` means another device
    answered the call; it is what the SDK reads as "answered by others".
    A header can list several protocols (`Q.850;cause=16, SIP;cause=200`);
    only the SIP one counts.
    """
    for entry in value.split(","):
        protocol, _, params = entry.partition(";")
        if protocol.strip().upper() != "SIP":
            continue
        cause = header_params(params).get("cause", "")
        if cause.isdigit():
            return int(cause)
    return None
