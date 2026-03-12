# Dreame H15 Pro — Architecture

## Overview

This integration communicates with the **Dreame Cloud API** to control and monitor the H15 Pro wet & dry floor cleaner. The API was reverse-engineered from the DreameHome mobile app (iOS/Android) using network traffic analysis.

---

## File structure

```text
dreame_h15pro/
├── __init__.py          Entry setup / teardown, platforms registration
├── api.py               Dreame Cloud API client (OAuth + device commands)
├── config_flow.py       UI config flow (refresh token → device picker)
├── coordinator.py       DataUpdateCoordinator (30s polling)
├── vacuum.py            Vacuum entity (StateVacuumEntity)
├── sensor.py            Sensor entities (battery, status, consumables, …)
├── switch.py            Switch entities (auto-clean, carpet boost)
├── select.py            Select entities (suction mode, water flow)
├── number.py            Number entities (water temperature setpoint)
├── binary_sensor.py     Binary sensor entities (charging, error)
├── const.py             Constants (API endpoints, property codes)
├── manifest.json
├── strings.json
├── translations/en.json
├── README.md
├── ARCHITECTURE.md      This file
└── icon.svg
```

---

## Authentication

The Dreame Cloud uses **OAuth 2.0** with long-lived refresh tokens.

```
POST https://id.dreame.tech/oauth/token
  grant_type=refresh_token
  refresh_token=<user-provided>
  client_id=<app client id>
→ { access_token, refresh_token, expires_in }
```

The user provides the initial refresh token (obtained by intercepting the DreameHome app's auth traffic). The integration stores it in the ConfigEntry and rotates it on each refresh (the refresh token is single-use).

**Token storage**: both `access_token` (in memory) and `refresh_token` (in ConfigEntry options) are managed by the API client.

---

## Device communication

After authentication, devices are fetched from the cloud:

```
GET https://api.dreame.tech/v1/user/devices
Authorization: Bearer <access_token>
→ [{ deviceId, deviceType, name, ... }]
```

Device state is polled via:

```
GET https://api.dreame.tech/v1/device/<deviceId>/properties
→ { properties: { <propCode>: <value>, ... } }
```

Commands are sent via:

```
POST https://api.dreame.tech/v1/device/<deviceId>/command
{ "command": "<cmd>", "params": { ... } }
```

---

## Property codes

The H15 Pro exposes its state as a flat map of integer property codes to values. Key codes:

| Code | Entity | Values |
| ---- | ------ | ------ |
| `2` | Status | 0=idle, 1=cleaning, 2=returning, 3=charging, 4=error |
| `3` | Battery | 0–100 (%) |
| `13` | Fan speed | 0=quiet, 1=standard, 2=strong, 3=max |
| `18` | Clean time | minutes |
| `19` | Clean area | m² × 10 |
| `20` | Water flow | 0–3 |
| `21` | Water temperature | °C |
| `101` | Filter life | 0–100 (%) |
| `102` | Roller life | 0–100 (%) |
| `103` | HEPA life | 0–100 (%) |
| `104` | Total runtime | hours |
| `105` | Total cleans | count |
| `106` | Total self-cleans | count |
| `107` | Last activity | ISO timestamp |
| `38` | Error code | device-specific integer |
| `33` | Auto self-clean | 0/1 |
| `34` | Carpet boost | 0/1 |

Constants are defined in `const.py` as `PROP_*` names to avoid magic numbers in entity code.

---

## Vacuum entity

`DreameH15ProVacuum(CoordinatorEntity, StateVacuumEntity)` maps HA vacuum states to Dreame status codes:

| HA state | Dreame status |
| -------- | ------------- |
| `cleaning` | 1 |
| `returning` | 2 |
| `docked` (charging) | 3 |
| `idle` | 0 |
| `error` | 4 |

Fan speed presets: `["quiet", "standard", "strong", "max"]` — mapped to codes 0–3.

Services supported: `start`, `stop`, `return_to_base`, `set_fan_speed`.

---

## Coordinator

`DreameH15ProCoordinator(DataUpdateCoordinator[DreameDeviceData])`:

- **Poll interval**: 30 seconds (device is cloud-connected, latency is fine)
- **Retry**: standard `DataUpdateCoordinator` exponential backoff on failure
- **Token refresh**: triggered when API returns 401; the new refresh token is persisted back to the ConfigEntry immediately

---

## Config flow

```
Step 1: user_input(refresh_token)
  → api.authenticate(refresh_token)
  → api.list_devices() → [H15Pro-A, H15Pro-B, …]
  → if single device: create entry immediately
  → if multiple: proceed to step 2

Step 2: user_input(device_id)
  → create entry with {refresh_token, device_id}
```

---

## Security notes

- The refresh token is stored in HA's config entry store (encrypted at rest if using the HA secrets system)
- The token is never logged
- All API calls use HTTPS
- This integration has no local network access to the device — all communication goes through the Dreame Cloud

---

## Future improvements

- Local protocol support (Dreame devices support a LAN protocol when on the same network)
- Map data from cleaning sessions
- Room-selective cleaning via segment commands
- Consumable replacement notifications via HA persistent notifications
