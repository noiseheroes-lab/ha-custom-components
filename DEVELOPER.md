# Developer Documentation — NoiseHeroes HA Custom Components

This document covers repository structure, development workflow, and contribution guidelines for the `ha-custom-components` monorepo.

---

## Repository structure

```text
ha-custom-components/
├── custom_components/
│   ├── octopus_energy_italy/   Cloud energy monitoring (Octopus Energy Italy)
│   ├── dreame_h15pro/          Robot floor cleaner (Dreame Cloud API)
│   ├── madoka_energy/          BLE energy sensors (Daikin Madoka BRC1H)
│   ├── vimar_intercom/         Video intercom (Vimar/Elvox SIP panels)
│   └── universal_audio/        Audio interface control (UA Apollo / UA Console)
├── .github/
│   └── workflows/
│       └── validate.yml        hassfest + HACS validation CI
├── hacs.json                   HACS repository metadata
├── README.md                   Public-facing integration index
├── DEVELOPER.md                This file
└── CHANGELOG.md                Version history per integration
```

Each integration is a fully self-contained Python package. They share no code and can be versioned, tested, and released independently. The monorepo structure is purely for convenience — HACS supports multiple `custom_components/` directories from a single repository.

---

## How HA custom components work

### Data flow

```
HA Core
  └── ConfigEntry (created by user via UI)
        └── DataUpdateCoordinator  ← polls external API on a schedule
              └── Entities (sensors, switches, …)  ← read coordinator.data
```

1. **ConfigEntry**: created when the user completes the config flow. Stores credentials and config in HA's storage.
2. **DataUpdateCoordinator**: a single object per entry that fetches all data from the external source on a schedule. All entities share one coordinator — this avoids N parallel API calls.
3. **Entities**: each entity reads from `coordinator.data` and exposes it as a HA state. Entities don't make network calls.

### Key HA patterns used

| Pattern | Purpose |
| ------- | ------- |
| `ConfigFlow` | Multi-step UI for authentication and device selection |
| `DataUpdateCoordinator` | Centralised polling with automatic error handling |
| `CoordinatorEntity` | Base class that wires entities to the coordinator |
| `SensorEntityDescription` | Declarative sensor definition with `value_fn` |
| `entity_registry_enabled_default` | Mark rarely-needed sensors as disabled by default |

---

## Development setup

### Prerequisites

- Python 3.12+
- Home Assistant installed locally or in a Docker container
- `hatch` or plain `pip` for dependency management

### Local testing

The fastest way to test a custom component is to symlink it into a local HA dev environment:

```bash
# Clone the repo
git clone https://github.com/noiseheroes-lab/ha-custom-components
cd ha-custom-components

# Symlink a component into your HA config directory
ln -s $(pwd)/custom_components/octopus_energy_italy \
      ~/.homeassistant/custom_components/octopus_energy_italy

# Start HA and add the integration via UI
hass -c ~/.homeassistant
```

For components that require real hardware (BLE, SIP, TCP) a full HA container on the target machine is needed.

### Running hassfest locally

`hassfest` validates `manifest.json` and entity definitions against HA's own rules:

```bash
python -m script.hassfest --action validate
```

Or via Docker (no HA source needed):

```bash
docker run --rm \
  -v $(pwd)/custom_components:/github/workspace/custom_components \
  ghcr.io/home-assistant/hassfest:latest
```

### HACS validation

```bash
pip install homeassistant-stubs hacs-action
python -m hacs_action validate
```

---

## Versioning

Each integration has its own `version` field in `manifest.json` and is versioned independently. Follow [Semantic Versioning](https://semver.org/):

- **MAJOR**: breaking change (entity IDs renamed, config entry migration required)
- **MINOR**: new entities or features, backwards compatible
- **PATCH**: bug fixes, no schema changes

Update `CHANGELOG.md` with every release using the format already established:

```markdown
## [x.y.z] — YYYY-MM-DD

### integration_domain vA.B.C

- What changed and why
```

---

## CI / GitHub Actions

`.github/workflows/validate.yml` runs on every push and PR:

1. **hassfest** — validates all `manifest.json` files and checks entity platform structure
2. **HACS** — validates the repository structure for HACS compatibility

Both must pass before merging. The workflow uses the standard actions published by the HA and HACS teams.

---

## Adding a new integration

1. Create `custom_components/<domain>/` with the required files:
   - `manifest.json` — domain, name, version, codeowners, iot_class, requirements
   - `__init__.py` — `async_setup_entry` + `async_unload_entry`
   - `config_flow.py` — UI flow (inherits `ConfigFlow`)
   - `coordinator.py` — `DataUpdateCoordinator` subclass
   - `sensor.py` (and other platforms) — entity definitions
   - `strings.json` + `translations/en.json` — UI strings
   - `README.md` — user-facing documentation
   - `ARCHITECTURE.md` — technical implementation notes
   - `icon.svg` — 256×256 integration icon
2. Add an entry to the root `README.md`
3. Add a release entry to `CHANGELOG.md`
4. Open a PR — CI will validate automatically

---

## Integration architecture docs

Each integration has a dedicated `ARCHITECTURE.md` in its directory:

- [Octopus Energy Italy — Architecture](custom_components/octopus_energy_italy/ARCHITECTURE.md)
- [Dreame H15 Pro — Architecture](custom_components/dreame_h15pro/ARCHITECTURE.md)
- [Daikin Madoka Energy — Architecture](custom_components/madoka_energy/ARCHITECTURE.md)
- [Vimar Intercom — Architecture](custom_components/vimar_intercom/ARCHITECTURE.md)
- [Universal Audio Apollo — Architecture](custom_components/universal_audio/ARCHITECTURE.md)

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
