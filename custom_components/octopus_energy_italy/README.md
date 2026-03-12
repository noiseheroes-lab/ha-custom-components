# Octopus Energy Italy — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue)](https://www.home-assistant.io)
[![Version](https://img.shields.io/github/v/release/noiseheroes-lab/ha-custom-components)](https://github.com/noiseheroes-lab/ha-custom-components/releases)

Monitor your **Octopus Energy Italy** electricity and gas consumption directly in Home Assistant — including live tariffs, monthly and yearly totals, and account balance. Works with the Energy Dashboard.

---

## Sensors

| Sensor | Unit | Description |
| ------ | ---- | ----------- |
| Electricity Yesterday | kWh | Previous day's electricity consumption |
| Electricity This Month | kWh | Accumulated electricity for the current billing month |
| Electricity This Year | kWh | Year-to-date electricity consumption |
| Electricity Rate | €/kWh | Current contracted sale rate |
| Electricity Standing Charge | €/year | Annual fixed charge |
| Gas This Month | Smc | Gas consumption this month |
| Gas This Year | Smc | Year-to-date gas consumption |
| Gas Rate | €/Smc | Current contracted gas rate |
| Gas Standing Charge | €/year | Annual fixed gas charge |
| Account Balance | € | Current account balance (positive = credit) |

All consumption sensors are compatible with the **Home Assistant Energy Dashboard**.

---

## Requirements

- Home Assistant 2024.1 or later
- An active [Octopus Energy Italy](https://octopusenergy.it) account
- Electricity and/or gas supply with Octopus Energy Italy

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/noiseheroes-lab/ha-custom-components` with category **Integration**
3. Install **Octopus Energy Italy**
4. Restart Home Assistant

### Manual

1. Copy `custom_components/octopus_energy_italy/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Octopus Energy Italy**
3. Enter your octopusenergy.it email and password
4. If you have multiple accounts, select the one to monitor

No YAML configuration needed — everything is done through the UI.

---

## Update frequency

Data is refreshed every **6 hours**. Consumption data from Octopus reflects the billing cycle — electricity is available as daily readings, gas as monthly meter readings from the distributor.

---

## Energy Dashboard

To add electricity consumption to the Energy Dashboard:

1. Go to **Settings → Dashboards → Energy**
2. Under **Electricity grid** → **Add consumption** → select **Electricity This Year** or **Electricity This Month**
3. Set the current tariff using the **Electricity Rate** sensor

---

## Known limitations

- Electricity granularity: daily and monthly (no half-hourly, even for smart meters — API limitation)
- Gas granularity: monthly (readings from the Italian distributor, typically available end of month)
- Data may lag 1–2 days behind actual consumption

---

## Technical notes

This integration uses the **Kraken GraphQL API** (`api.oeit-kraken.energy/v1/graphql/`) which is the same backend powering the Octopus Energy Italy web portal and mobile app. Authentication uses short-lived JWTs with automatic refresh.

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
