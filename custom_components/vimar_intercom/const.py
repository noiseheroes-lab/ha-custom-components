"""Constants for the Vimar Intercom integration.

Only true constants live here. Everything installation-specific comes
from the config entry through `runtime.RuntimeConfig`.
"""

import os

DOMAIN = "vimar_intercom"

# ─── Device info ─────────────────────────────────────────────────
MANUFACTURER = "Vimar"
MODEL = "Elvox Tab 5S Plus"

# ─── SIP protocol constants ──────────────────────────────────────
# The Vimar cloud is known to accept exactly this user agent. Changing
# it may break registration; it identifies the protocol dialect, not a
# specific installation.
USER_AGENT = ("TOGA_Googlesdk_gphone64_arm64_Android34"
              "/1.0|AppVer:2.4.0|ProtVer:1.0|")

# Push-notification contact parameters the Vimar cloud expects in the
# REGISTER Contact header. The token itself is generated per
# installation and stored in the config entry.
PN_APP_ID = "toga-prod"
PN_TYPE = "firebase"
MY_NAME = "Home Assistant"

SIP_LOCAL_PORT = 5070

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(SCRIPT_DIR, "vimar_rootca.pem")

# ─── Events ──────────────────────────────────────────────────────
EVENT_RING = "vimar_intercom_ring"

# ─── Repair issues ───────────────────────────────────────────────
ISSUE_REGISTRATION_DOWN = "registration_down"
REGISTRATION_DOWN_GRACE = 300  # seconds before raising the repair issue

# ─── Config entry keys ───────────────────────────────────────────
CONF_SIP_USER = "sip_user"
CONF_SIP_PASSWORD = "sip_password"
CONF_SIP_DOMAIN = "sip_domain"
CONF_CLOUD_PROXY = "cloud_proxy"
CONF_LOCAL_PROXY = "local_proxy"
CONF_GROUP_ID = "group_id"
CONF_MAC = "mac"
CONF_PLANT_TYPE = "plant_type"
CONF_PRODUCT_CODE = "product_code"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_UUID = "device_uuid"
CONF_PUSH_TOKEN = "push_token"

# ─── Options keys ────────────────────────────────────────────────
CONF_PANELS = "panels"
CONF_PREFER_LOCAL = "prefer_local"
CONF_RTP_PORT_BASE = "rtp_port_base"
CONF_SIP_PORT = "sip_port"
CONF_DOOR_COMMAND = "door_command"

# ─── Defaults ────────────────────────────────────────────────────
DEFAULT_CLOUD_PROXY = "ipvdes.vimar.cloud"
DEFAULT_SIP_PORT = 7042
DEFAULT_LOCAL_SIP_PORT = 5060
DEFAULT_GROUP_ID = "21"
DEFAULT_PANELS = "55001"
DEFAULT_DOOR_COMMAND = "OPEN_2F"
DEFAULT_RTP_PORT_BASE = 7200
DEFAULT_REGISTER_EXPIRY = 3600
MIN_REGISTER_EXPIRY = 60  # floor for a clamped registration lifetime, seconds
STABLE_CONNECTION_SECONDS = 60  # a connection must stay up this long before a fresh failure resets the backoff ladder
