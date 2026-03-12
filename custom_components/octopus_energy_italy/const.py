"""Constants for Octopus Energy Italy integration."""

DOMAIN = "octopus_energy_italy"
MANUFACTURER = "Octopus Energy Italy"

# API
KRAKEN_GQL = "https://api.oeit-kraken.energy/v1/graphql/"
TOKEN_TTL_SECONDS = 3600  # JWT expires in 1h

# Config entry keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ACCOUNT_NUMBER = "account_number"
CONF_REFRESH_TOKEN = "refresh_token"

# Data keys
DATA_COORDINATOR = "coordinator"

# Update intervals
UPDATE_INTERVAL_HOURS = 6

# Sensor unique ID suffixes
SENSOR_ELECTRICITY_RATE = "electricity_rate"
SENSOR_ELECTRICITY_STANDING = "electricity_standing_charge"
SENSOR_ELECTRICITY_MONTHLY = "electricity_monthly_kwh"
SENSOR_ELECTRICITY_YEARLY = "electricity_yearly_kwh"
SENSOR_ELECTRICITY_YESTERDAY = "electricity_yesterday_kwh"
SENSOR_GAS_RATE = "gas_rate"
SENSOR_GAS_STANDING = "gas_standing_charge"
SENSOR_GAS_MONTHLY = "gas_monthly_smc"
SENSOR_GAS_YEARLY = "gas_yearly_smc"
SENSOR_ACCOUNT_BALANCE = "account_balance"
