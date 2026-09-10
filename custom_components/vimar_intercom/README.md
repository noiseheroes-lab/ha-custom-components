# Vimar Intercom — Home Assistant Integration

> ### ⚠️ Version 1.x is in maintenance — v2 is under active development
>
> A substantial rewrite is in progress on the [`v2` branch](https://github.com/noiseheroes-lab/ha-custom-components/tree/v2).
> It is **not ready to install** and will be published as a pre-release when it is.
>
> Known limitations of the 1.x you are reading about, all fixed in v2:
>
> - **The component forces its own logger to DEBUG at import time**, which floods the Home Assistant
>   log and silently overrides your `logger:` configuration ([#1](https://github.com/noiseheroes-lab/ha-custom-components/issues/1)).
> - **Credentials and plant-specific values must be edited by hand in `const.py`.** v2 configures
>   itself from the QR code shown by the Vimar app.
> - **The entrance panel addresses are hardcoded to one specific installation**, and one of the two
>   defaults is wrong. They become configurable in v2.
> - **The camera entity never produces a frame.** Video is only forwarded to a private WebSocket
>   consumer, so the MJPEG view and the thumbnail are empty for everyone else.
> - **SIP reconnection gives up after five attempts** and stays down until Home Assistant restarts.
>
> If you run a Vimar Elvox plant and are willing to help test v2 once it is ready, please say so
> in an issue — this is reverse engineered against a single installation, and a second one is the
> only way to find out what has been assumed rather than established.

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
