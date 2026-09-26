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

# RFC 3263: a TLS SIP domain is located through this SRV service. The
# cloud proxy name in the QR is such a domain, not a host that listens.
SIP_SRV_SERVICE = "_sips._tcp"
SIP_SRV_LOOKUP_TIMEOUT = 10  # seconds for the whole SRV query
# Seconds one server gets for its TCP connect and TLS handshake together.
# Without a bound, a blackholed host stalled the supervisor silently, for
# as long as the kernel kept retrying the SYN.
SIP_CONNECT_TIMEOUT = 15
# RFC 8305's recommended stagger. A server name with several addresses,
# one of them unreachable, is otherwise tried an address at a time and
# can spend the whole connect timeout on the dead one.
SIP_HAPPY_EYEBALLS_DELAY = 0.25

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(SCRIPT_DIR, "vimar_rootca.pem")

# ─── Events ──────────────────────────────────────────────────────
EVENT_RING = "vimar_intercom_ring"
# A panel rang and nobody answered — reported by the indoor unit, or
# concluded from a ring the panel gave up on. Fired once per visitor.
EVENT_MISSED_CALL = "vimar_intercom_missed_call"
# A new video message is in the indoor unit's mailbox.
EVENT_VIDEO_MESSAGE = "vimar_intercom_video_message"

# ─── Dashboard card contract ─────────────────────────────────────
# State attributes the bundled dashboard card reads to tell the
# entities apart. The frontend sees neither unique IDs nor the entity
# plan, and names are the installer's, so every entity states what it
# is, and the per-panel buttons which panel they belong to. Public:
# other cards may rely on them too.
ATTR_INTERCOM_ROLE = "intercom_role"
ATTR_PANEL = "panel"
ATTR_PANEL_NAME = "panel_name"
ATTR_DEFAULT_PANEL = "default_panel"
ATTR_DEFAULT_PANEL_NAME = "default_panel_name"

ROLE_CAMERA = "camera"
ROLE_DOORBELL = "doorbell"
ROLE_REGISTRATION = "registration"
ROLE_IN_CALL = "in_call"
ROLE_ANSWER = "answer"
ROLE_HANGUP = "hangup"
ROLE_RECONNECT = "reconnect"
ROLE_CALL = "call"
ROLE_OPEN = "open"
ROLE_DOOR = "door"
ROLE_ACTUATOR = "actuator"
ROLE_DECLINE = "decline"
ROLE_RINGING = "ringing"
ROLE_DND = "dnd"
ROLE_VOICEMAIL = "voicemail"
ROLE_VOICEMAIL_TIMEOUT = "voicemail_timeout"
ROLE_MAILBOX_USAGE = "mailbox_usage"
ROLE_VIDEO_MESSAGES = "video_messages"
ROLE_MISSED_CALLS = "missed_calls"
ROLE_CAMERA_NEXT = "camera_next"
ROLE_CAMERA_PREVIOUS = "camera_previous"

# ─── Repair issues ───────────────────────────────────────────────
ISSUE_REGISTRATION_DOWN = "registration_down"
REGISTRATION_DOWN_GRACE = 300  # seconds before raising the repair issue
ISSUE_MIGRATION_REQUIRED = "migration_required"

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
DOOR_COMMAND_CURRENT = "OPEN_CURRENT"
DEFAULT_RTP_PORT_BASE = 7200
DEFAULT_REGISTER_EXPIRY = 3600
MIN_REGISTER_EXPIRY = 60  # floor for a clamped registration lifetime, seconds
STABLE_CONNECTION_SECONDS = 60  # a connection must stay up this long before a fresh failure resets the backoff ladder

# ─── Plant configuration (phonebook) ─────────────────────────────
# The indoor unit ("PICG") answers GET_INIT_STATUS. The SDK addresses it
# as 60001 on 2-wire V2 plants, cloud-only connections and VGIP units
# (RubricaDbManager.getPicg); other plants learn it from GET_NICKS,
# which this integration does not send yet.
PICG_ADDRESS = "60001"
# Seconds to wait for GET_INIT_STATUS_REPLY after asking for it.
PLANT_STATUS_TIMEOUT = 15
# The last good plant configuration, per entry, in `.storage`. It never
# holds the phonebook token.
PLANT_STORAGE_VERSION = 1
PLANT_STORAGE_KEY = "vimar_intercom.{entry_id}.plant"

# ─── Native-app features ─────────────────────────────────────────
# CALL_SWITCH_SOURCE goes to this address, whatever the plant: the SDK
# hardcodes it (VMSIPImpl.switchVideoSource).
CAMERA_SWITCH_ADDRESS = "60002"
# Seconds to wait for SET_APT_PARAMS_REPLY before calling a change failed.
APT_PARAMS_TIMEOUT = 10
# A ring nothing has ended after this many seconds is over anyway: a
# CANCEL can be lost with the connection, and the ringing sensor must not
# stay on for good. Panels give up ringing well before this.
RING_TIMEOUT = 120
# How many video messages the sensor lists in its attributes. The state
# attributes are written to the recorder on every change.
MAX_LISTED_VIDEO_MESSAGES = 50
# The local call log, per entry, in `.storage`.
CALL_LOG_STORAGE_VERSION = 1
CALL_LOG_STORAGE_KEY = "vimar_intercom.{entry_id}.call_log"
# Seconds a call-log change waits before it is written, so a burst of
# changes (a ring, its end, the unit's report) is one write.
CALL_LOG_SAVE_DELAY = 10

# ─── Services ────────────────────────────────────────────────────
SERVICE_MARK_VIDEO_MESSAGE_READ = "mark_video_message_read"
SERVICE_DELETE_VIDEO_MESSAGE = "delete_video_message"
SERVICE_DELETE_ALL_VIDEO_MESSAGES = "delete_all_video_messages"
SERVICE_PLAY_VIDEO_MESSAGE = "play_video_message"
SERVICE_CLEAR_MISSED_CALLS = "clear_missed_calls"
ATTR_MESSAGE_ID = "message_id"
