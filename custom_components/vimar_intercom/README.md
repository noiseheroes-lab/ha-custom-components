# Vimar Intercom — Home Assistant Integration

> **Status: in active development.** This is the v2 rewrite and it is not
> released yet. Do not point HACS at this branch. The stable version is 1.x
> on the `main` branch. This notice is removed when v2.0.0 ships.

Integrate a **Vimar Elvox** video door entry system into Home Assistant:
doorbell events, live video (with audio from the panel), door release and
call control, over the same cloud SIP protocol the Vimar View app uses.

## Verified hardware

Developed and tested against a Vimar Elvox Tab 5S Plus (40515/40517) on a
2-wire Due Fili Plus system. Other panels speak the same protocol and may
work, but are untested — please open an issue with your results.

## Before you install: only one registration exists per account

The Vimar cloud accepts exactly one SIP registration per account. A second
client registering with the same credentials — a test instance of this
integration, or the Vimar View app itself — deregisters the first one and
takes the intercom down in a real house. If you already run this
integration (or 1.x) somewhere, stop it before trying a second instance
anywhere else.

## Requirements

- The Vimar View app, already paired with your panel
- The QR configuration payload it can export (Settings → System → Export
  configuration)
- A Home Assistant host that can reach `ipvdes.vimar.cloud` on TCP 7042
- `ffmpeg`, bundled with Home Assistant OS and Home Assistant Container

## Installation

1. HACS → add `https://github.com/noiseheroes-lab/ha-custom-components`
   as a custom repository, then install **Vimar Intercom**. Or copy
   `custom_components/vimar_intercom` into your `config/custom_components`
   by hand.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Vimar Intercom.**

## Setup

Paste the QR payload from the Vimar View app. Confirm the summary. Done —
no panel IP, port or credential needs typing.

## Entities

| Entity | Type | Notes |
|---|---|---|
| `camera.vimar_intercom_intercom` | camera | Opening the stream places a call to the panel |
| `event.vimar_intercom_doorbell` | event | Event type `ring`, attribute `panel` |
| `lock.vimar_intercom_door` | lock | Unlock opens the main entrance; re-locks itself. Needs no configuration — it addresses the relay group from your QR code |
| `button.vimar_intercom_call_<panel>` | button | Call that panel |
| `button.vimar_intercom_open_<panel>` | button | Open that panel's door |
| `button.vimar_intercom_answer` / `_hangup` | button | Answer or end a call |
| `button.vimar_intercom_reconnect` | button | Rebuild the SIP connection |
| `binary_sensor.vimar_intercom_sip_registration` | binary_sensor | Connectivity; on only while registered |
| `binary_sensor.vimar_intercom_in_call` | binary_sensor | A call is up |

## The `vimar_intercom_ring` event

Every time a panel calls in, the integration fires `vimar_intercom_ring`
on the Home Assistant event bus. This is public API: the event name and
its payload will not be renamed without agreement, so it is safe to build
automations and companion apps against it.

Payload:

```json
{
  "panel": "<the calling panel's SIP address>",
  "panel_name": "<its configured name>",
  "entry_id": "<config entry id>"
}
```

```yaml
automation:
  - alias: "Notify on doorbell"
    triggers:
      - trigger: event
        event_type: vimar_intercom_ring
    actions:
      - action: notify.notify
        data:
          title: "Someone is at the door"
          message: "Panel {{ trigger.event.data.panel_name }} is calling."

  - alias: "Doorbell snapshot"
    triggers:
      - trigger: event
        event_type: vimar_intercom_ring
    actions:
      - action: camera.snapshot
        target:
          entity_id: camera.vimar_intercom_intercom
        data:
          filename: "/media/doorbell_{{ now().timestamp() | int }}.jpg"
```

## Options

- **Panel addresses** — `address:Name` pairs, comma separated (for
  example `55001:Street Gate, 55002:Building Door`). The address is the
  panel's SIP extension: it is printed on the panel's own address label
  and shown in the Vimar View app's address book. The field is pre-filled
  with a common default (`55001`), but check it against your own panel —
  it is not guaranteed to match your plant.
- **Door open command** — the SIP command sent to the door relay group
  outside a call. `OPEN_2F` by default; some plants want a different
  command.
- **Prefer local panel** — talk to the panel directly on the local
  network instead of through the Vimar cloud. Only takes effect if your
  QR code included a local panel address; there is no automatic fallback
  between the two.
- **Cloud SIP proxy port** — only applies when the local panel is *not*
  preferred; the local panel's own SIP port is fixed by the device and is
  not configurable.
- **RTP base port** — the local UDP port range used for the media
  streams.

## Limitations

- **You can see and hear the door; you cannot speak back.** Audio from
  the panel is carried in the camera stream. Home Assistant does not send
  audio to the panel, and has no two-way voice interface for cameras.
  Call and Answer control the call — they do not open a conversation.
- **One Vimar system per Home Assistant installation.** The integration
  declares `single_config_entry`.
- **The camera is not finished.** The SIP and RTP transport for video
  exists, but nothing yet delivers video frames to a Home Assistant
  consumer, so the camera entity currently shows no picture. This is
  tracked as follow-up work; remove this sentence once it ships and has
  been verified against a live panel.
- **Only one SIP registration exists per Vimar account.** Running a
  second client — a test instance, or the Vimar View app configured with
  the same credentials — will deregister this one.

## Upgrading from 1.x

This is not an upgrade path, it is a reinstall. There is no migration:
remove the 1.x config entry and add the integration again, pasting the
QR payload from the Vimar View app. Two things will otherwise look like
bugs:

- **Entity IDs change.** Per-panel buttons are now keyed by the panel's
  SIP address instead of the old `_ext` / `_int` suffixes. Any 1.x
  automation referencing the old entity IDs will need updating.
- **The two 1.x lock entities become one.** v2 has a single door lock.
  The old two entities are left behind in the entity registry, showing as
  unavailable, until you delete them by hand.

## Troubleshooting

- **Registration stays off** — check the host can reach the proxy shown
  in the sensor's attributes; press the Reconnect button. The integration
  retries forever, so a repair issue after five minutes means the panel
  or the network is the problem, not Home Assistant.
- **No video** — video only flows inside a call, so the camera is black
  until something opens the stream; check `ffmpeg` is present; check the
  RTP base port is not firewalled. See also the camera limitation above.
- **Door does not open** — the lock addresses the relay group from your
  QR code, so it should work untouched. While a call is up it opens the
  relay of the panel that is calling; otherwise it sends the configured
  door command, `OPEN_2F` by default. Some plants want a different
  command — change it in the options. For a second entrance, use that
  panel's door button.
- **More detail in the log:**

  ```yaml
  logger:
    logs:
      custom_components.vimar_intercom: debug
  ```

  DEBUG includes protocol traces. Do not leave it on.

## How it works, honestly

This integration speaks the SIP dialect the Vimar cloud uses for the
Vimar View app. That dialect is not documented; it was reverse engineered
from the app and from captured traffic. It works today and it can break
the day Vimar changes something. It does not use any Vimar partner API
and it is not affiliated with or endorsed by Vimar.

## License

MIT.
