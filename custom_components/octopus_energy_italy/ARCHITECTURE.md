# Octopus Energy Italy — Architecture

## Overview

This integration fetches energy consumption, tariff, and account data from the **Kraken GraphQL API** (`api.oeit-kraken.energy/v1/graphql/`) — the same backend used by the Octopus Energy Italy web portal and mobile app.

---

## File structure

```text
octopus_energy_italy/
├── __init__.py          Entry setup / teardown
├── api.py               Kraken API client (auth + data fetching)
├── config_flow.py       UI config flow (email + password → account picker)
├── coordinator.py       DataUpdateCoordinator (6h polling)
├── sensor.py            Sensor entity definitions
├── manifest.json
├── strings.json
├── translations/
│   ├── en.json
│   └── it.json
├── README.md
├── ARCHITECTURE.md      This file
└── icon.svg
```

---

## Authentication

The Kraken API uses **JWT-based auth** with short-lived access tokens and a long-lived refresh token.

```
POST https://api.oeit-kraken.energy/v1/graphql/
mutation obtainKrakenToken($email, $password)
→ { token, refreshToken }
```

- `token`: JWT, valid ~1 hour
- `refreshToken`: long-lived, non-rotating (does not expire on use)

The `OctopusEnergyItalyAPI` class stores both tokens in memory and automatically refreshes when the access token expires (detected via 401 response or by checking the JWT `exp` claim). Credentials are stored in the HA ConfigEntry — not in the API client.

**Refresh flow:**
```python
mutation refreshKrakenToken($refreshToken)
→ { token }  # returns new access token only
```

---

## Data model

### Config entry data

```python
{
    "email": "user@example.com",
    "password": "...",          # stored in HA secrets store
    "account_number": "A-XXXXX"
}
```

### `OctopusData` dataclass

```python
@dataclass
class OctopusData:
    electricity_yesterday_kwh: float | None
    electricity_monthly_kwh: float | None
    electricity_yearly_kwh: float | None
    electricity_rate: float | None        # €/kWh
    electricity_standing_charge: float | None  # €/year
    gas_monthly_smc: float | None
    gas_yearly_smc: float | None
    gas_rate: float | None               # €/Smc
    gas_standing_charge: float | None    # €/year
    account_balance: float | None        # € (positive = credit)
```

---

## API queries

### Electricity consumption

Electricity data is available as `DAY_INTERVAL` and `MONTH_INTERVAL` readings via the `measurements` field on a property:

```graphql
query ElectricityConsumption($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    properties {
      measurements(
        utilityFilters: [{
          electricityFilters: {
            marketSupplyPointId: "...",
            readingFrequencyType: DAY_INTERVAL,
            readingDirection: CONSUMPTION
          }
        }]
        startAt: "2024-01-01T00:00:00"
        endAt: "2024-12-31T23:59:59"
      ) {
        ... on ElectricityMeasurementType {
          readAt
          value
          unit
        }
      }
    }
  }
}
```

- `yesterday_kwh`: the most recent complete daily reading
- `monthly_kwh`: sum of daily readings in the current calendar month
- `yearly_kwh`: sum of daily readings in the current calendar year

### Gas consumption

Gas is reported as **cumulative meter readings** (Smc), not deltas. Monthly/yearly consumption is computed by subtracting consecutive readings:

```graphql
query GasReadings($accountNumber: String!, $pdr: String!) {
  gasMeterReadings(accountNumber: $accountNumber, pdr: $pdr, first: 24) {
    readAt
    value   # cumulative Smc
    unit
  }
}
```

Monthly delta: `readings[0].value - readings[1].value` (most recent minus previous).

### Tariffs

Tariff data is fetched from the account's agreements:

```graphql
query Tariffs($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    electricityAgreements {
      unitRate          # €/kWh
      standingCharge    # €/year
    }
    gasAgreements {
      unitRate          # €/Smc
      standingCharge    # €/year
    }
  }
}
```

### Account balance

```graphql
query Balance($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    balance   # integer, cents, negative = credit
  }
}
```

Converted to EUR: `abs(balance) / 100`. The sign is inverted (Octopus stores credit as negative, we expose it as positive credit).

---

## Coordinator

`OctopusEnergyCoordinator` extends `DataUpdateCoordinator[OctopusData]`.

- **Poll interval**: 6 hours (`UPDATE_INTERVAL = timedelta(hours=6)`)
- **Error handling**: if the API call fails, `DataUpdateCoordinator` retains the last known data and sets entity availability to `False` after `coordinator.last_update_success` is `False`
- **Token persistence**: the refresh token is stored in the ConfigEntry options and updated on each refresh

---

## Config flow

```
Step 1: user_input(email, password)
  → api.test_credentials()  [obtainKrakenToken]
  → if ok: api.list_accounts() → [A-XXXXX, A-YYYYY, …]
  → if single account: create entry immediately
  → if multiple: proceed to step 2

Step 2: user_input(account_number)
  → create entry with {email, password, account_number}
```

Errors caught: `AuthError` (wrong credentials), `APIError` (network/GraphQL error), `CannotConnect` (timeout).

---

## Known API limitations

| Limitation | Detail |
| ---------- | ------ |
| Electricity granularity | Daily minimum (no half-hourly for smart meters) |
| Gas granularity | Monthly (from distributor, lags 1–2 days) |
| Rate limiting | Not documented; 6h polling is conservative |
| Multiple supply points | Only the primary electricity and gas POD are exposed |

---

## Energy Dashboard compatibility

- `electricity_yesterday_kwh`: `state_class: measurement`, `device_class: energy`
- `electricity_monthly_kwh`: `state_class: total_increasing`, `device_class: energy`
- `electricity_yearly_kwh`: `state_class: total_increasing`, `device_class: energy`
- Gas sensors: same pattern but `unit_of_measurement: Smc`

---

## Future improvements

- Sub-daily electricity readings (if Octopus IT enables them)
- Push-based updates via Kraken webhooks (eliminates polling)
- Configurable poll interval
- Multiple supply point support
