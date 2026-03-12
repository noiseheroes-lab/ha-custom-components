# Dreame H15 Pro — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Control and monitor your **Dreame H15 Pro** wet & dry floor cleaner from Home Assistant. This integration uses the Dreame Cloud API (reverse-engineered from the DreameHome app) and provides full control over the device.

---

## Entities

### Vacuum

| Entity | Description |
| ------ | ----------- |
| `vacuum.dreame_h15pro` | Main vacuum entity — start, stop, return to base, set fan speed |

### Sensors

| Sensor | Description |
| ------ | ----------- |
| Status | Current device status (cleaning, idle, charging, error…) |
| Battery | Battery level (%) |
| Clean Time | Duration of current/last clean session (min) |
| Clean Area | Area cleaned in current/last session (m²) |
| Water Temperature | Self-cleaning water temperature (°C) |
| Total Runtime | Cumulative total runtime (h) |
| Total Cleans | Total number of clean sessions |
| Total Self-Cleans | Total number of self-cleaning cycles |
| Filter Life | Remaining filter life (%) |
| Roller Life | Remaining roller brush life (%) |
| HEPA Life | Remaining HEPA filter life (%) |
| Last Activity | Timestamp of last activity |
| Error / Warning | Current error or warning code |

### Switches

- Auto self-clean after session
- Carpet boost

### Selects

- Suction mode (Quiet / Standard / Strong / Max)
- Water flow level

### Numbers

- Self-clean water temperature setpoint

### Binary sensors

- Charging
- Error state

---

## Requirements

- Home Assistant 2024.1 or later
- Dreame H15 Pro registered in the **DreameHome** app
- Dreame OAuth refresh token (see below)

---

## Getting the Refresh Token

The integration uses the Dreame OAuth refresh token for authentication.

**Method:** Use the [dreame-vacuum token extractor](https://github.com/Tasshack/dreame-vacuum) or extract it from the DreameHome app traffic:

1. Log into the DreameHome app
2. Intercept the auth response (using a proxy like mitmproxy or Charles)
3. Locate the `refresh_token` in the `/oauth/token` response
4. Paste it in the integration config flow

---

## Installation

### Via HACS

1. Add `https://github.com/noiseheroes-lab/ha-custom-components` as a custom HACS repository
2. Install **Dreame H15 Pro**
3. Restart Home Assistant

### Manual

Copy `custom_components/dreame_h15pro/` to your HA `config/custom_components/`

---

## Configuration

1. Settings → Devices & Services → Add Integration → **Dreame H15 Pro**
2. Paste your refresh token
3. Select the device from the list

---

## Notes

- Cloud polling every 30 seconds
- The H15 Pro self-cleaning cycle is triggered automatically after each session (configurable)
- This integration is not affiliated with or endorsed by Dreame Technology

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
