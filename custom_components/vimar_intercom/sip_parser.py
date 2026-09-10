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
    do not echo the branch.
    """
    keys: list[str] = []
    branch = via_branch(msg.headers)
    seq, method = cseq_parts(msg.headers)
    if branch and seq is not None:
        keys.append(transaction_key(branch, seq, method))
    call_id = msg.headers.get("call-id", "")
    if call_id:
        keys.append(f"cid:{call_id}")
    return keys


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
