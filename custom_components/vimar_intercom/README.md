> **Moved.** Vimar Intercom is now developed and released at
> [noiseheroes-lab/ha-vimar-intercom](https://github.com/noiseheroes-lab/ha-vimar-intercom).
> Install it from there; this copy is no longer maintained.

# Vimar Intercom — Home Assistant Integration

> **Status: version 2.0.0.** `main` carries the v2 rewrite, so that is
> what HACS installs from the default branch. Registration, the plant
> phonebook, the doorbell, door release and live video with audio have
> been verified against a live Tab 5S Plus on a 2-wire plant. Upgrading
> from 1.x is a remove-and-re-add, not an in-place update.

Integrate a **Vimar Elvox** video door entry system into Home Assistant:
doorbell events, live video (with audio from the panel), door release and
call control, over the same cloud SIP protocol the Vimar View app uses.

## Hardware

Developed against a Vimar Elvox Tab 5S Plus (40515/40517) on a 2-wire Due
Fili Plus system. Other panels speak the same protocol and may work, but
are untested — please open an issue with your results.

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

Once registered, the integration asks the indoor unit for the plant's
*phonebook* — the configuration the installer loaded, which the Vimar
cloud publishes for the apps — and creates the entrance panels, door
locks and other actuators it lists, with the names set on the indoor
unit, just as the Vimar View app shows them. Nothing needs configuring.
It checks again on every reconnection and when the installer changes
the plant, rebuilds the entities if something changed (never in the
middle of a call), and removes the ones that no longer exist. The last
good copy is kept, so the entities are there even when the cloud is
not. Where no phonebook can be had — plant types that do not publish
one, or the cloud unreachable at first setup — the panel list and door
command in the options are used, as before.

If the indoor unit is on the same network, Home Assistant discovers it
(it announces itself over mDNS as `_eipvdes._tcp`) and offers to set it
up; confirming leads to the same QR step, since the announcement carries
no credentials. The QR code must be the discovered unit's own — one from
another unit is refused. For a unit already set up, a discovery only
refreshes its local address, which takes effect at the next restart.

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
| `lock.vimar_intercom_door` | lock | Unlock opens the main entrance; re-locks itself. Needs no configuration — it addresses the relay group from your QR code. With a phonebook it takes the installer's name for that door (see below) |
| `lock.vimar_intercom_<name>` | lock | One per door actuator in the phonebook (commands `OPEN_…`) |
| `button.vimar_intercom_<name>` | button | One per other actuator in the phonebook (`AUX…` outputs, lights) |
| `button.vimar_intercom_call_<panel>` | button | Call that panel |
| `button.vimar_intercom_open_<panel>` | button | Open that panel's door |
| `button.vimar_intercom_answer` / `_hang_up` | button | Answer or end a call |
| `button.vimar_intercom_reconnect` | button | Rebuild the SIP connection |
| `binary_sensor.vimar_intercom_sip_registration` | binary_sensor | Connectivity; on only while registered |
| `binary_sensor.vimar_intercom_in_call` | binary_sensor | A call is up |

Entities keep their IDs when the phonebook arrives. A panel's call and
open buttons stay the same entities, renamed after the phonebook. The
door lock becomes the phonebook door that is the same door — the
configured door command (`OPEN_2F` by default) on the panel your
apartment auto-switches to — and from then on always opens that door,
also during a call from another panel; use that panel's open button, or
its own lock, for the other entrance. If the phonebook has no such door,
the generic lock stays as it was. Door actuators are locks because a
door release is what Home Assistant's lock entity is for; `AUX` outputs
drive anything from a light to a second gate, so they are plain buttons.

## Dashboard card

The integration comes with a dashboard card: live video, the doorbell,
the doors and the other controls of your intercom in one place, laid
out like an intercom app. It is installed with the integration and
loaded on every dashboard automatically, so there is no resource to
add and nothing to install from HACS's frontend section. Edit a
dashboard, **Add card**, and pick **Vimar Intercom**; it finds your
intercom on its own.

```yaml
type: custom:vimar-intercom-card
# All optional:
title: Front door
device_id: <your intercom device>   # defaults to the first Vimar Intercom device
show_actuators: true                # lights and other AUX outputs
hidden_entities:                    # leave these out of the card
  - button.vimar_intercom_garage
```

The visual editor offers the same options. `hidden_entities` takes the
door locks, the controls, and a panel's call button (which removes that
panel from the panel selector) or open button (which removes Open door
from that panel's calls).

What it does:

- **Watch** shows the live stream of the default panel. As with the
  camera entity, opening the stream places the call, and the call ends
  shortly after the last viewer leaves. With more than one entrance
  panel, a panel selector appears: choosing another panel presses its
  Call button and shows the stream once the call is up. **Stop** ends a
  call the card placed or answered.
- **When a panel rings**, a banner names it, with **Answer**,
  **Dismiss** and **Open door**. The video does not start on its own:
  the panel sends no video until the call is answered, and opening the
  stream while it rings answers the call, so a wall tablet showing the
  card would otherwise take every visitor and stop the rest of the
  house ringing. Answer starts the video. Dismiss only hides the banner
  on this dashboard; the other devices keep ringing. During a call the
  card shows its duration, **Hang up**, and Open door for that panel.
- **Doors** are large buttons that need a confirmation: tap twice
  within three seconds, or press and hold. An opened door shows
  *Opened* for a moment.
- **Controls** (lights and other AUX outputs) are smaller buttons,
  pressed with a single tap.
- The footer shows whether the intercom is registered, with a
  **Reconnect** action when it is not.

The card follows your Home Assistant theme, light or dark, and is in
English or Italian after your profile language. Errors, such as a door
command with no reply, appear as a notification with the reason.

The card is told when a panel rings, not when it stops ringing or
another device answers, so an unanswered ring banner disappears after
30 seconds. A call answered anywhere else removes it straight away.

Every entity of the integration carries an `intercom_role` attribute
(`camera`, `doorbell`, `registration`, `in_call`, `answer`, `hangup`,
`reconnect`, `call`, `open`, `door`, `actuator`); call and open buttons
also carry `panel` and `panel_name`, and the camera `default_panel` and
`default_panel_name`. The card relies on them, and so can your own
cards and templates.

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

With a phonebook, panels, locks and actuators come from it and the first
two options below are fallbacks; the door command still decides which
phonebook door inherits the original door lock.

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
- **An auto-on view lasts as long as the panel allows** (about thirty
  seconds on the reference plant). The panel ends the call; watching
  again places a new one.
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
- **Panels or actuators missing, or still the ones from the options** —
  the phonebook could not be loaded; the log says why at WARNING. The
  indoor unit is asked at address 60001, which is where 2-wire V2 and
  cloud-connected plants have it; other plant types may not answer, and
  then the options are used.
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

  DEBUG includes protocol traces. Do not leave it on. The password of
  the phonebook download is never logged, at any level.

## How it works, honestly

This integration speaks the SIP dialect the Vimar cloud uses for the
Vimar View app. That dialect is not documented; it was reverse engineered
from the app and from captured traffic. It works today and it can break
the day Vimar changes something. It does not use any Vimar partner API
and it is not affiliated with or endorsed by Vimar.

## License

MIT.
