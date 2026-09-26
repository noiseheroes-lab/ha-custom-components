# Vimar Intercom — Home Assistant Integration

> **Status: version 2.0.0, in active development.** `main` carries the v2
> rewrite, so that is what HACS installs from the default branch. It has
> not been verified against a live panel — the camera in particular has
> never been run against one. Upgrading from 1.x is a remove-and-re-add,
> not an in-place update.

Integrate a **Vimar Elvox** video door entry system into Home Assistant:
doorbell events, live video (with audio from the panel), door release and
call control, over the same cloud SIP protocol the Vimar View app uses.

## Hardware

Developed against a Vimar Elvox Tab 5S Plus (40515/40517) on a 2-wire Due
Fili Plus system. Other panels speak the same protocol and may work, but
are untested — please open an issue with your results.

The camera has never been run against a live panel; see Limitations
below. Nothing in this repository claims it is verified.

## Before you install: only one registration exists per account

The Vimar cloud accepts exactly one SIP registration per account. A second
client registering with the same credentials — a test instance of this
integration, or the Vimar View app itself — deregisters the first one and
takes the intercom down in a real house. If you already run this
integration (or 1.x) somewhere, stop it before trying a second instance
anywhere else.

## Requirements

- Access to the indoor unit's settings, to generate a QR code for Home
  Assistant (Settings → Network and devices → Mobile device pairing)
- A Home Assistant host that can look up DNS SRV records and reach the
  Vimar SIP servers on TCP 7042. `ipvdes.vimar.cloud`, the name in the
  QR code, is a SIP domain rather than a server: its
  `_sips._tcp.ipvdes.vimar.cloud` SRV records name the servers to
  connect to, which the integration looks up on every connection
- `ffmpeg`, bundled with Home Assistant OS and Home Assistant Container

## Installation

1. HACS → add `https://github.com/noiseheroes-lab/ha-custom-components`
   as a custom repository, then install **Vimar Intercom**. Or copy
   `custom_components/vimar_intercom` into your `config/custom_components`
   by hand.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Vimar Intercom.**

## Setup

1. On the indoor unit, open **Settings → Network and devices → Mobile
   device pairing** and pick a **free slot**. The unit pairs up to ten
   devices and shows a QR code for the slot you pick — the same QR code
   the Vimar View app scans to pair a phone. Give Home Assistant a slot of
   its own: each slot has its own SIP identity. (Menu names are
   translated from the unit's Italian manual, *Impostazioni → Rete e
   dispositivi → Associazione dispositivo mobile*; your firmware's
   English labels may differ slightly.)
2. Take a photo or a screenshot of it, and upload it in the setup
   dialog. If you have the QR as text instead, paste it in the second
   field; when both are filled, the image is used.
3. Confirm the summary. Done — no panel IP, port or credential needs
   typing.

**Create a dedicated user for Home Assistant.** Each user has its own SIP
identity; sharing one with a phone or another system risks the two
knocking each other offline (see above).

The image is decoded on your Home Assistant, deleted as soon as it has
been read, and never logged: it *is* the credentials. The path above is
from a Tab 5S Plus; other indoor units may name the menu differently.

Reading the image needs `pyzbar` and the native `libzbar`. Home Assistant
OS and Container ship both, for core's own QR code integration. On a
Home Assistant Core install without `libzbar` the dialog says the reader
is unavailable, and pasting the text still works. iPhone photos in HEIC
format cannot be opened: upload a screenshot, or export the photo as
JPEG.

## Entities

| Entity | Type | Notes |
|---|---|---|
| `camera.vimar_intercom_intercom` | camera | Opening the stream places a call to the panel, or answers one that is ringing. Stills come from the call in progress; outside a call there is none |
| `event.vimar_intercom_doorbell` | event | Event type `ring`, attribute `panel` |
| `lock.vimar_intercom_door` | lock | Unlock opens the main entrance; re-locks itself. Needs no configuration — it addresses the relay group from your QR code |
| `button.vimar_intercom_call_<panel>` | button | Call that panel |
| `button.vimar_intercom_open_<panel>` | button | Open that panel's door |
| `button.vimar_intercom_answer` / `_hang_up` | button | Answer or end a call |
| `button.vimar_intercom_reconnect` | button | Rebuild the SIP connection |
| `binary_sensor.vimar_intercom_sip_registration` | binary_sensor | Connectivity; on only while registered |
| `binary_sensor.vimar_intercom_in_call` | binary_sensor | A call is up |

## The `vimar_intercom_ring` event

Every time a panel calls in, the integration fires `vimar_intercom_ring`
on the Home Assistant event bus. This is public API: the event name and
its payload will not be renamed without agreement, so it is safe to build
automations and companion apps against it.

It fires for a doorbell press that arrives while you are already on a
call, too. That call cannot be taken — the integration holds one call at
a time — so the second one is answered with a SIP 486 Busy Here and the
rest of the plant keeps ringing; but the event fires, so a notification
still reaches you. The one INVITE that fires nothing is the Vimar cloud
calling back the panel this integration just called itself, which is an
echo of your own call rather than a visitor.

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
      # The panel sends no video until the call is answered, so answer
      # it first. `camera.snapshot` on its own would find no call in
      # progress and no image to save.
      - action: button.press
        target:
          entity_id: button.vimar_intercom_answer
      - delay: "00:00:03"
      - action: camera.snapshot
        target:
          entity_id: camera.vimar_intercom_intercom
        data:
          filename: "/media/doorbell_{{ now().timestamp() | int }}.jpg"
```

Answering takes the call, exactly as pressing Answer in the Home
Assistant UI would: the panel stops ringing elsewhere in the house.

## Options

- **Panel addresses** — `address:Name` pairs, comma separated (for
  example `55001:Street Gate, 55002:Building Door`). The address is the
  panel's SIP extension: it is printed on the panel's own address label
  and shown in the Vimar View app's address book. The field is pre-filled
  with a common default (`55001`), but check it against your own panel —
  it is not guaranteed to match your plant.
- **Door open command** — the SIP command sent to the door relay group
  outside a call. `OPEN_2F` by default; some plants want a different
  command. Letters, digits and underscores only: it goes onto the wire
  exactly as written, and a space or an accented character would corrupt
  the connection to the panel. A command saved before this was checked
  falls back to `OPEN_2F`.
- **Prefer local panel** — talk to the panel directly on the local
  network instead of through the Vimar cloud. Only takes effect if your
  QR code included a local panel address; there is no automatic fallback
  between the two.
- **Cloud SIP proxy port** — only applies when the local panel is *not*
  preferred; the local panel's own SIP port is fixed by the device and is
  not configurable. Leave it at 7042 to use the port the Vimar cloud
  publishes in DNS. Any other value replaces that port for every
  server the SRV records name, or, where the proxy has no SRV records,
  is the port its name is dialled on.
- **RTP base port** — the local UDP port range used for the media
  streams.

## Limitations

- **You can see and hear the door; you cannot speak back.** Audio from
  the panel is carried in the camera stream. Home Assistant does not send
  audio to the panel, and has no two-way voice interface for cameras.
  Call and Answer control the call — they do not open a conversation.
- **One Vimar system per Home Assistant installation.** The integration
  declares `single_config_entry`.
- **The camera is implemented but not verified end to end against a live
  panel.** It streams the panel's H.264 video and PCMU audio, remuxed to
  MPEG-TS by ffmpeg, over a signed URL that Home Assistant's `stream`
  component fetches without a bearer token. The pipeline that feeds it —
  depacketisation, SPS/PPS replay, the ffmpeg consumer — is built and
  unit tested, but has never run against a real Vimar panel; see "Before
  you install" above for why that has not been tried yet. Remove this
  sentence once a validation session against real hardware confirms it.
- **There is no still image outside a call.** The panel only sends video
  inside a call, so a snapshot is taken from the call in progress, and
  outside one the camera has nothing to return. A still request never
  places a call of its own: a dashboard card polling the camera every
  ten seconds would otherwise ring the entrance panel every ten seconds.
- **A call is hung up after five minutes.** It is a safety net against a
  call nobody closes, and it applies to calls the camera placed as well
  as to answered ones. Open the stream again to place a new one.
- **The AV stream is reachable by any authenticated Home Assistant
  user.** Entity permissions do not apply to an HTTP view, so hiding the
  camera entity from a user does not stop them fetching
  `/api/vimar_intercom/av` with their own token — and fetching it places
  a call to the panel. This is how Home Assistant views work rather than
  something this integration introduces, but it is worth knowing if you
  have non-admin users.
- **Only one SIP registration exists per Vimar account.** Running a
  second client — a test instance, or the Vimar View app configured with
  the same credentials — will deregister this one.

## Upgrading from 1.x

This is not an upgrade path, it is a reinstall. There is no migration:
remove the 1.x config entry and add the integration again with the QR
code generated on the indoor unit (see Setup). Two things will otherwise
look like bugs:

- **Nearly every entity ID changes, not just the per-panel buttons.**
  1.x entities carried no device-name prefix and used Italian names —
  for example `binary_sensor.intercom_sip`, `event.doorbell`,
  `button.rispondi`, `button.riaggancia`, `camera.intercom`, and
  `lock.street_gate` / `lock.building_door`. v2 prefixes every entity
  with `vimar_intercom_` and uses English names throughout — see the
  Entities table above for the current IDs. Per-panel buttons are also
  keyed differently: they are now keyed by the panel's SIP address
  instead of the old `_ext` / `_int` suffixes. Any 1.x automation
  referencing the old entity IDs will need rewriting, not patching.
- **The two 1.x lock entities become one.** v2 has a single door lock.
  The old two entities are left behind in the entity registry, showing as
  unavailable, until you delete them by hand.

## Troubleshooting

- **"That QR code is not a Vimar one, or it is incomplete"** — as well
  as a payload that will not decrypt, this covers one that decrypts to
  values the integration will not put on the wire: the SIP user and the
  relay group must be alphanumeric, and the domain and the proxy
  addresses must be host names or IP addresses. The same checks apply
  whether the QR came from an image or from pasted text. Only use a QR
  code your own indoor unit generated.
- **"No QR code was found in that image"** — use a sharp, straight-on
  photo or a screenshot with the whole QR code in view, or paste the
  text.
- **Registration stays off** — the log names each SIP server tried and
  why it failed. `ipvdes.vimar.cloud` is not itself a SIP server, so a
  direct connection to it failing proves nothing; check the host can
  resolve its `_sips._tcp` SRV records and reach the servers they name,
  then press the Reconnect button. The integration retries forever, so
  a repair issue after five minutes means the panel or the network is
  the problem, not Home Assistant.
- **No video** — video only flows inside a call, so the camera is black
  until something opens the stream; check `ffmpeg` is present; check the
  RTP base port is not firewalled. See also the camera limitation above.
- **A snapshot saves nothing** — there is no image outside a call.
  Answer or place a call first; see the "Doorbell snapshot" automation
  above.
- **The video cut out after five minutes** — that is the maximum call
  duration, not a fault. Open the stream again.
- **Door does not open** — the lock addresses the relay group from your
  QR code, so it should work untouched. While a call is up it opens the
  relay of the panel that is calling; otherwise it sends the configured
  door command, `OPEN_2F` by default. Some plants want a different
  command — change it in the options. For a second entrance, use that
  panel's door button. A failure is now reported as an error on the
  action, with the reason, rather than only written to the log — so an
  automation can catch it and the UI shows it.
- **"No reply from the intercom. The door may have opened anyway"** —
  the command went out and nothing came back within the timeout. The
  integration deliberately does not resend it: a command that reached
  the panel and only lost its acknowledgement would pulse the relay a
  second time, so the door would open, close and open again unattended.
  Check the door before pressing again.
- **"The Vimar CA certificate is missing"** — the integration refuses to
  set up without `vimar_rootca.pem`, because the connection it verifies
  with it is the one that carries the door command. Reinstall through
  HACS. It previously carried on without verifying the certificate at
  all.
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
