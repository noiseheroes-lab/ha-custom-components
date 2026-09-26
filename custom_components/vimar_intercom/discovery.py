"""What the indoor unit advertises on the local network, parsed.

The indoor unit announces itself over mDNS as `_eipvdes._tcp.local.`
with three TXT keys (the SDK's `VMSIPConstants.MDNS_KEY_*`): `mac`, its
MAC address; `proxy`, the address of its local SIP proxy; and `domain`,
its local SIP domain. The official app looks the service up by the MAC
from the QR code and takes `proxy` (and `domain`) from it to reach the
unit on the home network (`VMSIPImpl.findDnsServiceByMac`).

Discovery here only tells Home Assistant that a unit is there and which
one: the credentials still come from the QR code, which the unit alone
can show. `domain` is not used — the integration talks to the cloud SIP
domain from the QR — so it is not required.

Nothing here imports Home Assistant, so every decision the config flow
makes about a discovery is unit tested directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .runtime import valid_host, valid_mac

SERVICE_TYPE = "_eipvdes._tcp.local."
TXT_MAC = "mac"
TXT_PROXY = "proxy"
TXT_DOMAIN = "domain"
MAX_NAME_LENGTH = 64

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class DiscoveredUnit:
    """One indoor unit seen on the network."""

    mac: str  # normalised, see `normalize_mac`
    host: str
    name: str


def normalize_mac(value: str | None) -> str | None:
    """`AA:BB:CC:DD:EE:FF`, or None if `value` is not a MAC address.

    The QR and the TXT record may disagree on case and separator; the
    SDK compares them case-insensitively. Normalising is how two spellings
    of one unit compare equal here.
    """
    if not value or not valid_mac(value.strip()):
        return None
    return value.strip().upper().replace("-", ":")


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)


def parse_discovery(
    properties: Mapping[str, Any], address: str, service_name: str
) -> DiscoveredUnit:
    """Build a DiscoveredUnit from a zeroconf announcement.

    Raises ValueError without a usable `mac`: without it the unit cannot
    be told apart from another one, nor matched to its QR code. A
    missing or malformed `proxy` falls back to the address the
    announcement came from, which is the unit itself.
    """
    props = {_text(k).lower(): _text(v).strip() for k, v in properties.items()}
    mac = normalize_mac(props.get(TXT_MAC))
    if mac is None:
        raise ValueError("the announcement carries no valid MAC address")
    proxy = props.get(TXT_PROXY, "")
    host = proxy if valid_host(proxy) else address
    if not valid_host(host):
        raise ValueError("the announcement carries no usable address")
    name = service_name.split(f".{SERVICE_TYPE}")[0].split("._eipvdes")[0]
    name = _CONTROL_RE.sub(" ", name).strip()[:MAX_NAME_LENGTH] or host
    return DiscoveredUnit(mac=mac, host=host, name=name)


def configured_unique_id(
    mac: str, unique_ids: Iterable[str | None]
) -> str | None:
    """The unique ID of an existing entry for this unit, if there is one.

    Entries set up from a QR code carry its MAC as written in the QR,
    and nothing guarantees the TXT record spells it the same way, so the
    comparison is on the normalised form. The entry's own spelling is
    returned so the flow can match it exactly.
    """
    for unique_id in unique_ids:
        if unique_id and normalize_mac(unique_id) == mac:
            return unique_id
    return None


def qr_matches_discovery(discovered_mac: str, qr_mac: str | None) -> bool:
    """True if the QR code read after a discovery belongs to that unit.

    A QR without a MAC cannot be shown to belong to it, so it does not.
    """
    return normalize_mac(qr_mac) == discovered_mac
