# Daikin Madoka Energy — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Read energy consumption data from the **Daikin Madoka BRC1H** smart thermostat via Bluetooth Low Energy. Exposes kWh sensors compatible with the Home Assistant **Energy Dashboard**.

---

## Sensors

| Sensor | Unit | Description |
| ------ | ---- | ----------- |
| Energy Today | kWh | Electricity consumed today |
| Energy Yesterday | kWh | Electricity consumed yesterday |
| Energy This Week | kWh | This week's total |
| Energy Last Week | kWh | Last week's total |
| Energy This Year | kWh | Year-to-date total |
| Energy Last Year | kWh | Previous year's total |

All sensors have `state_class: total` and `device_class: energy` — compatible with the **Energy Dashboard**.

---

## Requirements

- Home Assistant 2024.1 or later with **Bluetooth** integration enabled
- Daikin BRC1H Madoka thermostat with Bluetooth active
- The HA host must have Bluetooth hardware within range (~10m)

---

## Installation

### Via HACS

1. Add `https://github.com/noiseheroes-lab/ha-custom-components` as a custom HACS repository
2. Install **Daikin Madoka Energy**
3. Restart Home Assistant

### Manual

Copy `custom_components/madoka_energy/` to your HA `config/custom_components/`

---

## Configuration

1. Settings → Devices & Services → Add Integration → **Daikin Madoka Energy**
2. HA will scan for nearby Madoka devices automatically
3. Select your thermostat (or enter the MAC address manually)
4. Confirm

---

## Notes

- Data is read via BLE every 5 minutes
- The Madoka keeps internal counters — data persists across HA restarts
- Supports multiple Madoka thermostats (add each as a separate integration entry)
- This integration reads energy data only; for full thermostat control see the native Daikin integration

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
