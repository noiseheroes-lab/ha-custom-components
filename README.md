# Noise Heroes — Home Assistant Custom Components

A collection of Home Assistant custom integrations for devices and services not yet natively supported — built and maintained by [Noise Heroes](https://github.com/noiseheroes-lab).

---

## Integrations

### 🐙 [Octopus Energy Italy](custom_components/octopus_energy_italy/)

Monitor electricity and gas consumption, live tariffs, and account balance for **Octopus Energy Italy** customers. Compatible with the Energy Dashboard.

**Entities:** electricity/gas consumption (daily · monthly · yearly), current rates, standing charges, account balance

**IoT class:** Cloud polling · **Version:** 1.0.0

---

### 🤖 [Dreame H15 Pro](custom_components/dreame_h15pro/)

Full control and monitoring for the **Dreame H15 Pro** wet & dry floor cleaner via Dreame Cloud API.

**Entities:** vacuum control, battery, clean time/area, water temp, consumable life (filter · roller · HEPA), error/warning

**IoT class:** Cloud polling · **Version:** 2.0.0

---

### 🌡️ [Daikin Madoka Energy](custom_components/madoka_energy/)

Read energy consumption data from the **Daikin Madoka BRC1H** smart thermostat via Bluetooth. Energy Dashboard compatible.

**Entities:** energy today, yesterday, this week, last week, this year, last year (kWh)

**IoT class:** Local polling (BLE) · **Version:** 0.1.0

---

### 🔔 Vimar Intercom — moved to [noiseheroes-lab/ha-vimar-intercom](https://github.com/noiseheroes-lab/ha-vimar-intercom)

Vimar Intercom 2.0 lives in its own repository, so HACS can install it
(HACS allows one integration per repository). Install and report issues
there. The copy in this repository is no longer maintained.

**IoT class:** Local push (SIP) · **Version:** 2.0.0

---

### 🎛️ [Universal Audio Apollo](custom_components/universal_audio/)

Control your **Universal Audio Apollo** audio interface from HA via the UA Console TCP protocol. Monitor volume, phantom power, sample rate.

**Entities:** media_player, monitor level, phantom power, Hi-Z, phase, sample rate

**IoT class:** Local push (TCP) · **Version:** 1.0.0

---

## Installation via HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. URL: `https://github.com/noiseheroes-lab/ha-custom-components` — Category: **Integration**
3. Install the integration you want
4. Restart Home Assistant

---

## Structure

```text
ha-custom-components/
├── custom_components/
│   ├── octopus_energy_italy/
│   ├── dreame_h15pro/
│   ├── madoka_energy/
│   ├── vimar_intercom/
│   └── universal_audio/
├── hacs.json
├── CHANGELOG.md
└── README.md
```

Each integration can be developed and versioned independently.

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
