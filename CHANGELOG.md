# Changelog

## [1.1.0] — 2026-03-12

### dreame_h15pro v2.0.0

- Initial public release (ported from private HA install)
- Vacuum, sensors, switches, selects, number, binary sensors
- Dreame Cloud API with OAuth token auth

### madoka_energy v0.1.0

- Initial public release
- BLE polling for energy consumption (today/yesterday/week/year)
- Bluetooth auto-discovery + manual MAC entry
- Energy Dashboard compatible

### vimar_intercom v1.3.0

- Initial public release
- SIP stack integration for Vimar Elvox panels
- Camera, event, lock, button, binary_sensor entities
- Local push, no cloud dependency

### universal_audio v1.0.0

- Initial public release
- UA Console TCP protocol integration
- media_player, monitor volume, phantom power, Hi-Z, phase, sample rate

---

## [1.0.0] — 2026-03-12

### octopus_energy_italy

#### Added
- Initial release
- Authentication via Kraken GraphQL API (`api.oeit-kraken.energy`) with JWT + auto-refresh
- Electricity consumption sensors: yesterday, this month, this year (kWh)
- Gas consumption sensors: this month, this year (Smc)
- Tariff sensors: electricity rate (€/kWh), gas rate (€/Smc), standing charges
- Account balance sensor (€)
- Config flow UI: email + password → auto-discover accounts and supply points
- Italian and English translations
- Energy Dashboard compatible (`state_class: total_increasing`)
- HA device grouping — all sensors under one device per account
