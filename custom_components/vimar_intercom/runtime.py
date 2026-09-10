"""Every runtime value the integration needs, derived from the config entry.

Nothing here imports Home Assistant, so it can be unit tested directly.
`RuntimeConfig` is immutable: rebuild it when the entry changes rather
than mutating it.
"""

from __future__ import annotations

import hashlib
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
AV_VIDEO_PORT_OFFSET = 12000
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
    av_video_port: int
    av_audio_port: int
    user_agent: str

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


def entry_data_from_qr(fields: Mapping[str, str]) -> dict[str, Any]:
    """Build the config entry `data` dict from decoded QR fields.

    The device identity is generated once, here, and then reused for the
    life of the entry. It must never be a constant in source.
    """
    return {
        CONF_SIP_USER: fields["ID"],
        CONF_SIP_PASSWORD: fields["PWD"],
        CONF_SIP_DOMAIN: fields["CDOMAIN"],
        CONF_CLOUD_PROXY: fields.get("CPROXY") or DEFAULT_CLOUD_PROXY,
        CONF_LOCAL_PROXY: fields.get("PROXY", ""),
        CONF_GROUP_ID: fields.get("GID") or DEFAULT_GROUP_ID,
        CONF_MAC: fields.get("MAC", ""),
        CONF_PLANT_TYPE: fields.get("PLANTTYPE", ""),
        CONF_PRODUCT_CODE: fields.get("PC", ""),
        CONF_DEVICE_ID: "".join(
            secrets.choice("0123456789") for _ in range(DEVICE_ID_DIGITS)),
        CONF_DEVICE_UUID: str(uuid.UUID(bytes=secrets.token_bytes(16), version=4)),
        CONF_PUSH_TOKEN: secrets.token_hex(32),
    }


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
    else:
        proxy_host = cloud_proxy
        proxy_port = int(options.get(CONF_SIP_PORT, DEFAULT_SIP_PORT))

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
        door_command=options.get(CONF_DOOR_COMMAND) or DEFAULT_DOOR_COMMAND,
        rtp_audio_port=rtp_base,
        rtp_video_port=rtp_base + VIDEO_PORT_OFFSET,
        av_video_port=rtp_base + AV_VIDEO_PORT_OFFSET,
        av_audio_port=rtp_base + AV_AUDIO_PORT_OFFSET,
        user_agent=USER_AGENT,
    )
