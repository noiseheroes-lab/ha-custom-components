"""Constants for Vimar Intercom integration."""

import os

DOMAIN = "vimar_intercom"

# ─── Device Info ──────────────────────────────────────────────────
MANUFACTURER = "Vimar"
MODEL = "Elvox Tab5S Plus"

# ─── SIP ─────────────────────────────────────────────────────────
SIP_USER = "REDACTED_SIP_USER"
SIP_DOMAIN = "YOUR_SIP_DOMAIN"
SIP_PASSWORD = "YOUR_SIP_PASSWORD"
SIP_HA1 = "YOUR_SIP_HA1"
SIP_PROXY = "YOUR_SIP_PROXY"
SIP_PORT = 7042
SIP_SNI = "ipvdes.vimar.cloud"
SIP_ROUTE = "ipvdes.vimar.cloud"
USER_AGENT = ("TOGA_Googlesdk_gphone64_arm64_Android34"
              "/1.0|AppVer:2.4.0|ProtVer:1.0|")

# Push-notification contact parameters the Vimar cloud expects in the
# REGISTER Contact header. These are protocol constants, not credentials —
# the token itself is generated per installation and stored in the entry.
PN_APP_ID = "toga-prod"
PN_TYPE = "firebase"
MY_NAME = "Home Assistant"

# Identity fields the Vimar cloud protocol requires on REGISTER, MESSAGE
# and INVITE (Mobile-IMEI header, +sip.instance URN). Not real hardware
# identifiers — placeholders, since this integration does not run on a
# physical device with its own IMEI.
DEVICE_IMEI = "REDACTED_DEVICE_IMEI"
DEVICE_UUID = DEVICE_IMEI

# VoIP push token for the REGISTER Contact header. Left empty: this
# integration no longer sends mobile push notifications. SIP registration
# works the same without it — sip_client.py falls back to a shorter
# Contact expiry and omits the pn-tok Contact params.
PN_TOKEN = ""

INTERCOM = f"sip:55001@{SIP_DOMAIN}"

# ─── Door targets (from Tab5S rubrica ACTUATOR_LIST) ─────────────
# Messages go to the targa (PE) address, NOT to relay 60002/60003.
# The targa forwards the command to its local relay.
DOOR_ESTERNO = f"sip:55001@{SIP_DOMAIN}"   # Portone Esterno → targa master
DOOR_INTERNO = f"sip:55002@{SIP_DOMAIN}"   # Portone Interno → targa interna
DOOR_COMMAND = "OPEN_2F"                     # ATT_ID 8 = 2F Module (Serratura)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(SCRIPT_DIR, "vimar_rootca.pem")

# ─── RTP / Media ─────────────────────────────────────────────────
RTP_AUDIO_PORT = 7200
RTP_VIDEO_PORT = 9200
FFMPEG_VIDEO_PORT = 19200       # MJPEG ffmpeg reads video here
FFMPEG_AV_VIDEO_PORT = 19201    # AV ffmpeg reads video here
FFMPEG_AV_AUDIO_PORT = 19202    # AV ffmpeg reads audio here

# ─── Local Tab5S ─────────────────────────────────────────────────
LOCAL_PROXY = "192.168.X.X"
LOCAL_SIP_PORT = 5060

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
