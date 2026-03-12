# Daikin Madoka Energy — Architecture

## Overview

This integration reads energy consumption data from the **Daikin BRC1H Madoka** smart thermostat over Bluetooth Low Energy (BLE). The Madoka stores cumulative kWh counters internally and exposes them via a proprietary GATT service.

---

## File structure

```text
madoka_energy/
├── __init__.py          Entry setup / teardown
├── ble_client.py        BLE connection + GATT read logic (via bleak)
├── config_flow.py       UI config flow (BLE device discovery → confirm)
├── coordinator.py       DataUpdateCoordinator (5min polling)
├── sensor.py            Sensor entity definitions (6 energy sensors)
├── const.py             GATT UUIDs and energy counter offsets
├── manifest.json
├── strings.json
├── translations/en.json
├── README.md
├── ARCHITECTURE.md      This file
└── icon.svg
```

---

## BLE protocol

The Madoka BRC1H exposes a proprietary GATT service with UUID:

```
2141e110-213a-11e6-b67b-9e71128cae77
```

This UUID is declared in `manifest.json` under the `bluetooth` key, which tells HA's Bluetooth integration to watch for advertisements from this device.

### Connection flow

```
1. Discover device (passive scan via HA Bluetooth)
2. Connect via bleak.BleakClient(address)
3. Read energy characteristic(s)
4. Parse binary payload → kWh values
5. Disconnect
```

Connections are short-lived — connect, read, disconnect. The Madoka does not maintain a persistent BLE connection.

### Data encoding

The energy payload is a binary struct. Each counter is a 4-byte little-endian float or fixed-point integer (device firmware dependent):

| Offset | Counter |
| ------ | ------- |
| 0 | Energy today (kWh × 10) |
| 4 | Energy yesterday (kWh × 10) |
| 8 | Energy this week (kWh × 10) |
| 12 | Energy last week (kWh × 10) |
| 16 | Energy this year (kWh × 10) |
| 20 | Energy last year (kWh × 10) |

Values are divided by 10 to get the actual kWh float. The Madoka maintains these counters across power cycles — HA restarts don't reset them.

---

## Bluetooth discovery

The config flow uses HA's built-in BLE discovery:

```python
class MadokaConfigFlow(ConfigFlow):
    async def async_step_bluetooth(self, discovery_info):
        # Called automatically when a Madoka is detected nearby
        # Shows confirmation dialog with device name + MAC
        ...

    async def async_step_user(self, user_input):
        # Manual entry: user types MAC address directly
        ...
```

The `manifest.json` `bluetooth` entry ensures HA passes Madoka advertisements directly to this integration's config flow.

---

## Coordinator

`MadokaEnergyCoordinator(DataUpdateCoordinator[MadokaEnergyData])`:

- **Poll interval**: 5 minutes
- **BLE retry**: BLE connections are unreliable; the coordinator retries up to 3 times before raising `UpdateFailed`
- **Multiple devices**: each device gets its own ConfigEntry and coordinator — there is no cross-device state

### `MadokaEnergyData` dataclass

```python
@dataclass
class MadokaEnergyData:
    energy_today_kwh: float
    energy_yesterday_kwh: float
    energy_this_week_kwh: float
    energy_last_week_kwh: float
    energy_this_year_kwh: float
    energy_last_year_kwh: float
```

---

## Sensor entities

All sensors share the same base config: `device_class: energy`, `unit: kWh`. `state_class` varies:

| Sensor | `state_class` | Notes |
| ------ | ------------- | ----- |
| Energy Today | `measurement` | Resets daily |
| Energy Yesterday | `measurement` | Fixed until midnight |
| Energy This Week | `measurement` | Resets weekly |
| Energy Last Week | `measurement` | Fixed |
| Energy This Year | `total_increasing` | Suitable for Energy Dashboard long-term stat |
| Energy Last Year | `measurement` | Fixed |

---

## HA Bluetooth integration dependency

This integration depends on HA's built-in `bluetooth` integration being set up and active. The host machine must have a working Bluetooth adapter. On Raspberry Pi (common HA host), the built-in adapter works. On Docker/macOS, a Bluetooth USB dongle or host Bluetooth passthrough is needed.

---

## Future improvements

- Full thermostat control (target temperature, HVAC mode) — requires reverse engineering the control characteristic
- Indoor temperature and humidity sensor read (exposed on a separate GATT characteristic)
- Push-based BLE notifications instead of polling (if the device supports GATT notify on the energy characteristic)
- Support for BRC1H02 variant
