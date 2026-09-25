# Vimar Intercom — Home Assistant Integration

> ### 🚧 Version 2 is under active development on `main`
>
> This is a reverse-engineered integration in the middle of a rewrite, and `main` is the
> development branch. Everything here compiles and the unit tests pass, but v2 is **not
> feature-complete** and has **not been verified against a clean Home Assistant install**.
> Install it only if you are willing to help shape it.
>
> **Working in v2 today**
> - Setup by pasting the QR code shown by the Vimar app — no more editing `const.py`
> - Every credential and plant-specific value lives in the config entry
> - Entrance panel addresses are configurable instead of hardcoded
> - SIP responses correlated per transaction, so REGISTER replies are no longer discarded
> - Apple-specific push code and the private-app HTTP surface removed
>
> **Not done yet**
> - Unbounded SIP reconnection and a real registration lifecycle (1.x gave up after five attempts)
> - The public `vimar_intercom_ring` event and the reconnect / repair affordances
> - The camera: video is not yet forwarded to a Home Assistant consumer, so the entity produces no frame
> - Final documentation, changelog and the English-only sweep
>
> **Upgrading from 1.x requires reconfiguring the integration**, because credentials moved out of
> `const.py` into the config entry. If you need the previous behaviour, install from commit
> [`883deac`](https://github.com/noiseheroes-lab/ha-custom-components/tree/883deac).
>
> If you run a Vimar Elvox plant, testing reports are the most useful thing you can contribute:
> this is reverse engineered against a single installation, and a second one is the only way to
> find out what has been assumed rather than established.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Integrate your **Vimar Elvox** video intercom panel into Home Assistant. Receive doorbell events, view the camera feed, answer calls, and unlock doors — all natively.

Compatible with: **Elvox Tab 5S Plus**, Vimar VIEW IP panels, and other Vimar/Elvox SIP-based intercoms.

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Videocitofono | `camera` | Live RTSP video feed from the door panel |
| Campanello | `event` | Fires when the doorbell is pressed |
| Serratura | `lock` | Open the door lock / gate |
| Chiama | `button` | Initiate an outbound call to the panel |
| Riaggancia | `button` | Hang up the current call |
| Registrazione SIP | `binary_sensor` | SIP registration status |
| In Chiamata | `binary_sensor` | Active call indicator |

---

## Requirements

- Home Assistant 2024.1 or later
- Vimar/Elvox intercom panel on the **local network** (SIP over LAN)
- SIP credentials for the panel (IP, user, password, extension)
- ffmpeg installed on the HA host (for camera stream)

---

## Installation

### Via HACS
1. Add `https://github.com/noiseheroes-lab/ha-custom-components` as a custom HACS repository
2. Install **Vimar Intercom**
3. Restart Home Assistant

### Manual
Copy `custom_components/vimar_intercom/` to your HA `config/custom_components/`

---

## Configuration

1. Settings → Devices & Services → Add Integration → **Vimar Intercom**
2. Enter the SIP configuration for your panel:
   - Panel IP address
   - SIP extension and password
   - RTSP stream URL (if different from default)

---

## How it works

The integration runs a lightweight SIP stack that registers with the intercom panel. When the doorbell is pressed, the panel initiates a SIP call — HA captures this as an `event` entity and triggers automations. The camera feed is exposed via RTSP.

Door unlocking is sent as a SIP MESSAGE to the panel.

---

## Automations example

```yaml
automation:
  - alias: "Doorbell notification"
    trigger:
      - platform: event
        event_type: vimar_intercom_campanello
    action:
      - service: notify.mobile_app_iphone
        data:
          message: "Someone at the door!"
          data:
            actions:
              - action: "OPEN_DOOR"
                title: "Open"
```

---

## Notes

- Local push — no cloud dependency
- SIP registration is maintained persistently; binary_sensor shows live status
- This integration is not affiliated with or endorsed by Vimar S.p.A.

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
