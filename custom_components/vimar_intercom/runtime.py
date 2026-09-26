"""Every runtime value the integration needs, derived from the config entry.

Nothing here imports Home Assistant, so it can be unit tested directly.
`RuntimeConfig` is immutable: rebuild it when the entry changes rather
than mutating it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_CLOUD_PROXY,
    CONF_DEVICE_ID,
    CONF_DEVICE_UUID,
    CONF_DOOR_COMMAND,
    CONF_GROUP_ID,
    CONF_LOCAL_PROXY,
    CONF_MAC,
    CONF_PANELS,
    CONF_PLANT_TYPE,
    CONF_PREFER_LOCAL,
    CONF_PRODUCT_CODE,
    CONF_PUSH_TOKEN,
    CONF_RTP_PORT_BASE,
    CONF_SIP_DOMAIN,
    CONF_SIP_PASSWORD,
    CONF_SIP_PORT,
    CONF_SIP_USER,
    DEFAULT_CLOUD_PROXY,
    DEFAULT_DOOR_COMMAND,
    DEFAULT_GROUP_ID,
    DEFAULT_LOCAL_SIP_PORT,
    DEFAULT_PANELS,
    DEFAULT_RTP_PORT_BASE,
    DEFAULT_SIP_PORT,
    USER_AGENT,
)

DEVICE_ID_DIGITS = 15
VIDEO_PORT_OFFSET = 2000
AV_AUDIO_PORT_OFFSET = 12002


@dataclass(frozen=True)
class PanelConfig:
    """One entrance panel, addressed by its SIP extension."""

    address: str
    name: str


@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable view of the config entry, with every derived value."""

    sip_user: str
    sip_password: str = field(repr=False)
    sip_domain: str
    proxy_host: str
    proxy_port: int
    sni: str
    route: str
    local_proxy: str
    local_sip_port: int
    prefer_local: bool
    group_id: str
    mac: str
    plant_type: str
    product_code: str
    device_id: str = field(repr=False)
    device_uuid: str = field(repr=False)
    push_token: str = field(repr=False)
    panels: tuple[PanelConfig, ...]
    door_command: str
    rtp_audio_port: int
    rtp_video_port: int
    av_audio_port: int
    user_agent: str
    # True for the cloud proxy, whose name is a SIP domain located through
    # SRV; False for the panel's LAN address, which is dialled as given.
    locate_by_srv: bool = False
    # The options flow's port when it differs from the default. It then
    # replaces the port the SRV records advertise.
    proxy_port_override: int | None = None

    @property
    def sip_ha1(self) -> str:
        """MD5(user:realm:password), the digest-auth HA1 for this account."""
        raw = f"{self.sip_user}:{self.sip_domain}:{self.sip_password}"
        return hashlib.md5(raw.encode()).hexdigest()

    @property
    def default_panel(self) -> PanelConfig:
        """The panel used when a call or door command names no target."""
        return self.panels[0]

    def panel_uri(self, address: str) -> str:
        """SIP URI for a panel extension."""
        return f"sip:{address}@{self.sip_domain}"

    @property
    def door_uri(self) -> str:
        """The door relay group, taken from the QR's GID field."""
        return f"sip:{self.group_id}@{self.sip_domain}"

    @property
    def account_uri(self) -> str:
        """SIP URI of this Home Assistant account."""
        return f"sip:{self.sip_user}@{self.sip_domain}"

    @property
    def registrar_uri(self) -> str:
        """Request URI used by REGISTER."""
        return f"sip:{self.sip_domain}"


def parse_panels(raw: str) -> tuple[PanelConfig, ...]:
    """Parse a comma-separated panel list.

    Each item is `address` or `address:Friendly Name`. Addresses must be
    numeric SIP extensions. Raises ValueError on an empty or malformed list.
    """
    panels: list[PanelConfig] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        address, _, name = item.partition(":")
        address = address.strip()
        name = name.strip()
        if not address.isdigit():
            raise ValueError(f"'{address}' is not a numeric SIP extension")
        panels.append(PanelConfig(address, name or f"Panel {address}"))

    if not panels:
        raise ValueError("at least one panel address is required")
    return tuple(panels)


# A SIP extension or relay group as it may appear in a URI's user part.
# Deliberately narrower than RFC 3261 allows: every value this
# integration has ever seen is digits, and nothing wider is needed to
# talk to a Vimar plant.
_SIP_TOKEN_RE = re.compile(r"\A[A-Za-z0-9]{1,32}\Z")

# One hostname label, per RFC 1123. A dotted-quad IP address matches
# this too, which is what the local panel address usually is.
_HOST_LABEL_RE = re.compile(r"\A[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

# A MAC address as the Vimar app writes it. Only ever used as the config
# entry's unique ID and shown back to the user in the setup dialog.
_MAC_RE = re.compile(r"\A[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}\Z")


def valid_sip_token(value: str) -> bool:
    """True if `value` is safe as a SIP URI's user part (an extension)."""
    return bool(_SIP_TOKEN_RE.match(value or ""))


def _checked_token(value: str, field: str) -> str:
    """Return `value` if it is safe in a SIP URI's user part."""
    if not _SIP_TOKEN_RE.match(value):
        raise ValueError(
            f"the QR field '{field}' is not a valid SIP extension")
    return value


def _checked_host(value: str, field: str) -> str:
    """Return `value` if it is a hostname or an IP address."""
    if not value or len(value) > 253:
        raise ValueError(f"the QR field '{field}' is not a valid host name")
    if not all(_HOST_LABEL_RE.match(label) for label in value.split(".")):
        raise ValueError(f"the QR field '{field}' is not a valid host name")
    return value


def _checked_mac(value: str) -> str:
    """Return `value` if it is a MAC address, or "" if it is absent."""
    if value and not _MAC_RE.match(value):
        raise ValueError("the QR field 'MAC' is not a valid MAC address")
    return value


# The door command is sent verbatim as the body of a SIP MESSAGE, so it
# is held to the shape of the commands the panels actually accept
# (OPEN_2F, OPEN_CURRENT, OPEN_1F...). That excludes the CR and LF that
# would forge a second request, and the non-ASCII characters that would
# make Content-Length under-count the body and corrupt the stream.
_DOOR_COMMAND_RE = re.compile(r"\A[A-Za-z0-9_]{1,32}\Z")


def valid_door_command(command: str) -> bool:
    """True if `command` is safe to send as a SIP MESSAGE body."""
    return bool(_DOOR_COMMAND_RE.match(command or ""))


def entry_data_from_qr(fields: Mapping[str, str]) -> dict[str, Any]:
    """Build the config entry `data` dict from decoded QR fields.

    Every value that reaches a SIP message is checked against a strict
    charset here and rejected, not escaped — the same thing
    `parse_panels` does for panel addresses, and for the same reason.
    `qr.parse_fields` percent-decodes what it reads, so `%0D%0A` in a QR
    field arrives as a real CRLF; interpolated into an f-string that
    builds a request it forges whole SIP headers, and `CPROXY` is both
    the connection host and the TLS SNI, so one hostile payload could
    redirect the entire session. Reaching this needs the user to paste a
    payload they were given, which is exactly what people do with
    configuration snippets swapped on a forum.

    Rejecting also keeps the values safe to render: the confirm step
    shows the SIP user, the domain and the MAC as markdown.

    Raises ValueError, which the config flow reports as `invalid_qr`. The
    message names the field and never the value, so nothing decrypted
    from the payload can reach a log or a dialog.

    The device identity is generated once, here, and then reused for the
    life of the entry. It must never be a constant in source.
    """
    return {
        CONF_SIP_USER: _checked_token(fields["ID"], "ID"),
        CONF_SIP_PASSWORD: fields["PWD"],
        CONF_SIP_DOMAIN: _checked_host(fields["CDOMAIN"], "CDOMAIN"),
        CONF_CLOUD_PROXY: _checked_host(
            fields.get("CPROXY") or DEFAULT_CLOUD_PROXY, "CPROXY"),
        CONF_LOCAL_PROXY: (
            _checked_host(fields["PROXY"], "PROXY")
            if fields.get("PROXY") else ""),
        CONF_GROUP_ID: _checked_token(
            fields.get("GID") or DEFAULT_GROUP_ID, "GID"),
        CONF_MAC: _checked_mac(fields.get("MAC", "")),
        CONF_PLANT_TYPE: fields.get("PLANTTYPE", ""),
        CONF_PRODUCT_CODE: fields.get("PC", ""),
        CONF_DEVICE_ID: "".join(
            secrets.choice("0123456789") for _ in range(DEVICE_ID_DIGITS)),
        CONF_DEVICE_UUID: str(uuid.UUID(bytes=secrets.token_bytes(16), version=4)),
        CONF_PUSH_TOKEN: secrets.token_hex(32),
    }


def _door_command_of(options: Mapping[str, Any]) -> str:
    """The configured door command, or the default if it is unusable.

    The options flow validates this now, but an entry saved before it did
    can still hold anything. Falling back to the default keeps the
    intercom working; refusing to load would leave the door unreachable
    over a field the user can no longer reach except through the same
    options flow.
    """
    command = options.get(CONF_DOOR_COMMAND) or DEFAULT_DOOR_COMMAND
    if not valid_door_command(command):
        return DEFAULT_DOOR_COMMAND
    return command


def build_runtime_config(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> RuntimeConfig:
    """Combine entry data and options into an immutable RuntimeConfig."""
    cloud_proxy = data.get(CONF_CLOUD_PROXY) or DEFAULT_CLOUD_PROXY
    local_proxy = data.get(CONF_LOCAL_PROXY, "")
    prefer_local = bool(options.get(CONF_PREFER_LOCAL, False)) and bool(local_proxy)

    if prefer_local:
        # The panel's own SIP port is fixed by the device. CONF_SIP_PORT
        # configures the cloud proxy only: the options flow always
        # persists it, so applying it here would aim a saved cloud port
        # at the local panel.
        proxy_host = local_proxy
        proxy_port = DEFAULT_LOCAL_SIP_PORT
        port_override = None
    else:
        proxy_host = cloud_proxy
        proxy_port = int(options.get(CONF_SIP_PORT, DEFAULT_SIP_PORT))
        # The options flow always saves this field, so a stored default
        # cannot be told apart from one typed in. Only a value that
        # differs from the default is read as the user's choice; the
        # default itself defers to whatever port SRV publishes.
        port_override = proxy_port if proxy_port != DEFAULT_SIP_PORT else None

    rtp_base = int(options.get(CONF_RTP_PORT_BASE, DEFAULT_RTP_PORT_BASE))

    return RuntimeConfig(
        sip_user=data[CONF_SIP_USER],
        sip_password=data[CONF_SIP_PASSWORD],
        sip_domain=data[CONF_SIP_DOMAIN],
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        sni=cloud_proxy,
        route=cloud_proxy,
        local_proxy=local_proxy,
        local_sip_port=DEFAULT_LOCAL_SIP_PORT,
        prefer_local=prefer_local,
        group_id=data.get(CONF_GROUP_ID) or DEFAULT_GROUP_ID,
        mac=data.get(CONF_MAC, ""),
        plant_type=data.get(CONF_PLANT_TYPE, ""),
        product_code=data.get(CONF_PRODUCT_CODE, ""),
        device_id=data[CONF_DEVICE_ID],
        device_uuid=data[CONF_DEVICE_UUID],
        push_token=data[CONF_PUSH_TOKEN],
        panels=parse_panels(options.get(CONF_PANELS) or DEFAULT_PANELS),
        door_command=_door_command_of(options),
        rtp_audio_port=rtp_base,
        rtp_video_port=rtp_base + VIDEO_PORT_OFFSET,
        av_audio_port=rtp_base + AV_AUDIO_PORT_OFFSET,
        user_agent=USER_AGENT,
        locate_by_srv=not prefer_local,
        proxy_port_override=port_override,
    )
