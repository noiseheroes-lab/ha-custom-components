"""Constants for the Dreame H15 Pro integration."""

DOMAIN = "dreame_h15pro"

# Dreame Cloud API
API_BASE_URL = "https://eu.iot.dreame.tech:13267"
CLIENT_ID = "dreame_appv1"
CLIENT_SECRET = "AP^dv@z@SQYVxN88"
BASIC_AUTH = "ZHJlYW1lX2FwcHYxOkFQXmR2QHpAU1FZVnhOODg="
TENANT_ID = "000000"

# Token refresh before expiry (seconds)
TOKEN_REFRESH_MARGIN = 600  # 10 minutes before expiry

# Polling interval
DEFAULT_SCAN_INTERVAL = 30  # seconds

# ── MIoT property keys (siid.piid) ──────────────────────────────────

# siid 2: Device status
PROP_STATUS = "2.1"

# siid 3: Battery
PROP_BATTERY = "3.1"

# siid 4: Cleaning service
PROP_WORK_MODE = "4.1"
PROP_CLEAN_TIME = "4.2"
PROP_CLEAN_AREA = "4.3"
PROP_SUCTION_LEVEL = "4.5"
PROP_WATER_TANK = "4.6"
PROP_TIMER = "4.7"
PROP_CAPABILITY = "4.38"
PROP_CAPABILITY_EXT = "4.83"

# siid 6: Statistics
PROP_TOTAL_RUNTIME = "6.7"

# siid 1: Device settings & info
PROP_VOICE_PROMPT = "1.7"
PROP_CHILD_LOCK = "1.10"
PROP_DND_START = "1.12"
PROP_DND_END = "1.13"
PROP_CACHED_STATUS = "1.28"
PROP_ERROR_CODE = "1.29"
PROP_WARN_CODE = "1.30"
PROP_SELF_CLEAN_TEMP = "1.33"
PROP_DRYING_TEMP = "1.34"
PROP_AUTO_DRYING = "1.35"
PROP_DRYING_DURATION = "1.36"
PROP_LAST_ACTIVITY = "1.47"
PROP_CLEANING_MODE = "1.49"
PROP_DRYING_TIME = "1.50"
PROP_SELF_CLEAN_MODE = "1.51"
PROP_UNKNOWN_1_52 = "1.52"
PROP_WATER_TEMP = "1.54"
PROP_TOTAL_CLEANS = "1.56"
PROP_TOTAL_SELF_CLEANS = "1.57"
PROP_FILTER_LIFE = "1.64"
PROP_ROLLER_LIFE = "1.65"
PROP_HEPA_LIFE = "1.66"
PROP_CONSUMABLE_1_68 = "1.68"
PROP_CONSUMABLE_1_69 = "1.69"
PROP_CONSUMABLE_1_70 = "1.70"
PROP_CONSUMABLE_1_71 = "1.71"
PROP_AUTO_ADD_WATER = "1.73"

# siid 7: Voice/audio
PROP_VOICE_PACK = "7.7"

# siid 16: Sensor/consumable tracking
PROP_SENSOR_DIRTY_LEVEL = "16.1"
PROP_SENSOR_DIRTY_TIME = "16.2"
PROP_SENSOR_16_6 = "16.6"
PROP_SENSOR_16_7 = "16.7"

# siid 19: Additional stats
PROP_RUNTIME_SECONDARY = "19.3"

# All properties to poll
ALL_PROPS = [
    PROP_STATUS,
    PROP_BATTERY,
    PROP_WORK_MODE,
    PROP_CLEAN_TIME,
    PROP_CLEAN_AREA,
    PROP_SUCTION_LEVEL,
    PROP_WATER_TANK,
    PROP_TIMER,
    PROP_CAPABILITY,
    PROP_CAPABILITY_EXT,
    PROP_TOTAL_RUNTIME,
    PROP_VOICE_PROMPT,
    PROP_CHILD_LOCK,
    PROP_DND_START,
    PROP_DND_END,
    PROP_ERROR_CODE,
    PROP_WARN_CODE,
    PROP_SELF_CLEAN_TEMP,
    PROP_DRYING_TEMP,
    PROP_AUTO_DRYING,
    PROP_DRYING_DURATION,
    PROP_LAST_ACTIVITY,
    PROP_CLEANING_MODE,
    PROP_DRYING_TIME,
    PROP_SELF_CLEAN_MODE,
    PROP_UNKNOWN_1_52,
    PROP_WATER_TEMP,
    PROP_TOTAL_CLEANS,
    PROP_TOTAL_SELF_CLEANS,
    PROP_FILTER_LIFE,
    PROP_ROLLER_LIFE,
    PROP_HEPA_LIFE,
    PROP_CONSUMABLE_1_68,
    PROP_CONSUMABLE_1_69,
    PROP_CONSUMABLE_1_70,
    PROP_CONSUMABLE_1_71,
    PROP_AUTO_ADD_WATER,
    PROP_VOICE_PACK,
    PROP_SENSOR_DIRTY_LEVEL,
    PROP_SENSOR_DIRTY_TIME,
    PROP_SENSOR_16_6,
    PROP_SENSOR_16_7,
    PROP_RUNTIME_SECONDARY,
]

# ── Device status enum (2.1 values) ─────────────────────────────────

STATUS_MAP = {
    1: "mopping",
    2: "offline",
    3: "standby",
    4: "charging",
    5: "self_cleaning",
    6: "drying",
    7: "sleeping",
    8: "vacuuming",
    9: "adding_water",
    10: "mopping_paused",
    11: "self_cleaning_paused",
    12: "drying_paused",
    13: "updating",
    14: "updating_voice",
    15: "charging_complete",
    16: "mopping",
    17: "mopping",
    18: "mopping",
    19: "mopping",
    20: "mopping",
    21: "mopping",
    22: "mopping",
    23: "drying",
    24: "drying",
    25: "drying",
    26: "hot_water_self_cleaning",
    27: "hot_water_self_cleaning",
    28: "hot_water_self_cleaning",
    29: "vacuuming_paused",
    30: "self_cleaning_paused",
    31: "self_cleaning_paused",
    32: "drying",
    33: "drying",
    40: "mopping",
    41: "mopping",
}

# Status display names (Italian)
STATUS_DISPLAY = {
    "mopping": "Lavaggio",
    "offline": "Offline",
    "standby": "Standby",
    "charging": "In carica",
    "self_cleaning": "Autopulizia",
    "drying": "Asciugatura",
    "sleeping": "Riposo",
    "vacuuming": "Aspirazione",
    "adding_water": "Aggiunta acqua",
    "mopping_paused": "Lavaggio in pausa",
    "self_cleaning_paused": "Autopulizia in pausa",
    "drying_paused": "Asciugatura in pausa",
    "updating": "Aggiornamento",
    "updating_voice": "Aggiornamento voce",
    "charging_complete": "Carica completata",
    "hot_water_self_cleaning": "Autopulizia acqua calda",
    "vacuuming_paused": "Aspirazione in pausa",
}

# Cleaning statuses (for binary sensor and session tracking)
CLEANING_STATUSES = {
    "mopping",
    "vacuuming",
    "self_cleaning",
    "hot_water_self_cleaning",
    "drying",
    "adding_water",
}

# Error codes
ERROR_MAP = {
    0: None,
    1: "Sensore caduta",
    2: "Sensore dirupo",
    3: "Paraurti bloccato",
    4: "Spazzola bloccata",
    5: "Serbatoio sporco pieno",
    6: "Serbatoio sporco mancante",
    7: "Serbatoio acqua vuoto",
    8: "Serbatoio acqua mancante",
    9: "Filtro intasato",
    10: "Rullo aggrovigliato",
    11: "Spazzola laterale aggrovigliata",
    12: "Dispositivo bloccato",
    13: "Batteria scarica",
}

# Warning codes
WARN_MAP = {
    0: None,
    1: "Acqua in esaurimento",
    2: "Acqua sporca piena",
}

# ── Config flow keys ─────────────────────────────────────────────────

CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_EXPIRY = "token_expiry"
CONF_DEVICE_DID = "device_did"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_MODEL = "device_model"

# ── Events ───────────────────────────────────────────────────────────

EVENT_CLEANING_STARTED = f"{DOMAIN}_cleaning_started"
EVENT_CLEANING_FINISHED = f"{DOMAIN}_cleaning_finished"
EVENT_SELF_CLEAN_STARTED = f"{DOMAIN}_self_clean_started"
EVENT_SELF_CLEAN_FINISHED = f"{DOMAIN}_self_clean_finished"
