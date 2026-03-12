# Universal Audio Apollo — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Control and monitor your **Universal Audio Apollo** audio interface from Home Assistant via the **UA Console** TCP protocol.

Supports Apollo Solo, Twin, Twin X, x4, x6, x8, x8p, x16 — any Apollo connected to UA Console on macOS.

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| Apollo | `media_player` | Volume control, mute, source selection |
| Monitor Output | `number` | Monitor output level (dB) |
| Input Gain | `number` | Preamp input gain |
| Sample Rate | `sensor` | Current session sample rate (Hz) |
| Phantom Power | `switch` | 48V phantom power per channel |
| Hi-Z | `switch` | Hi-Z instrument input |
| Phase | `switch` | Phase invert per channel |

---

## Requirements

- Home Assistant 2024.1 or later
- Universal Audio Apollo interface
- **UA Console** running on macOS on the local network
- UA Console must have "Remote Control" enabled

---

## Installation

### Via HACS
1. Add `https://github.com/noiseheroes-lab/ha-custom-components` as a custom HACS repository
2. Install **Universal Audio Apollo**
3. Restart Home Assistant

### Manual
Copy `custom_components/universal_audio/` to your HA `config/custom_components/`

---

## Configuration

1. Settings → Devices & Services → Add Integration → **Universal Audio Apollo**
2. Enter:
   - **Host**: IP address of the Mac running UA Console
   - **Port**: TCP port (default: 4710)
   - **Device index**: Apollo index (0 for first device)
   - **Monitor output index**: Which output to expose as media_player

---

## Use cases

- Control studio monitor volume from HA dashboard or voice assistant
- Automate mute when a video call starts
- Include UA volume in whole-home audio scenes
- Physical display widget for current monitor level

---

## Notes

- Local push — communicates directly with UA Console over TCP, no cloud
- UA Console must be open and connected to the Apollo
- This integration is not affiliated with or endorsed by Universal Audio, Inc.

---

MIT © [Noise Heroes](https://github.com/noiseheroes-lab)
