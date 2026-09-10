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
