# Vimar Intercom — Home Assistant Integration

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
