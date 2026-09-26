# Vimar Intercom — Architecture

## Overview

This integration speaks the SIP dialect the Vimar cloud uses for the
Vimar View app. It is not a generic SIP client for local Vimar/Elvox
panels: the panel is reached through Vimar's own cloud proxy (the SIP
domain `ipvdes.vimar.cloud` by default, whose servers are located through
DNS SRV on port 7042), the same path the phone app uses,
using a user agent string and REGISTER/INVITE shape the cloud is known to
accept. This dialect is not documented by Vimar; it was reverse
engineered from the app and from captured traffic. It works today and it
can break the day Vimar changes something on their end.

## Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | Entry setup/teardown, wires the hub to Home Assistant, stores the plant configuration and removes stale entities, registers the AV stream HTTP view |
| `hub.py` | Orchestrates SIP registration, calls, door control and media lifecycle; the only module that talks to both `sip_client` and Home Assistant |
| `sip_client.py` | The SIP stack itself: connection, digest auth, REGISTER/INVITE/BYE/MESSAGE, transaction correlation, reconnection |
| `sip_parser.py` | Pure text handling: header parsing, transaction keys, registration expiry parsing — no I/O, no Home Assistant |
| `sip_locate.py` | Where the SIP socket goes: RFC 3263 SRV lookup of the cloud proxy domain (via `aiodns`, a Home Assistant core requirement), RFC 2782 ordering, and trying each server in turn under a connect timeout — no Home Assistant |
| `backoff.py` | The jittered exponential reconnect delay schedule |
| `qr.py` | Reads the QR code from an uploaded image (pyzbar, imported lazily) and decrypts and parses the configuration payload the indoor unit generates |
| `runtime.py` | `RuntimeConfig` — every value the integration needs, derived once from the config entry and the stored plant configuration; no Home Assistant import, fully unit testable |
| `system_messages.py` | The indoor unit's system messages: GET_INIT_STATUS_REPLY and NEW_PHONEBOOK parsing, and a classifier for every line kind the SDK knows — no I/O, no Home Assistant |
| `plant_config.py` | Phonebook SQLite bytes → frozen `PlantConfig` (panels, our apartment group, actuators, SYSTEM parameters), selected as the SDK's queries select them; its JSON-safe stored form — no Home Assistant |
| `phonebook.py` | The phonebook download: URL, RFC 7616 digest auth, a bounded fetch over a duck-typed aiohttp session — no Home Assistant |
| `discovery.py` | The indoor unit's mDNS announcement (`_eipvdes._tcp`, TXT `mac`/`proxy`/`domain`): parsing, MAC normalisation, matching it to an existing entry and to the QR read afterwards — no Home Assistant |
| `entity_plan.py` | Which plant-dependent entities exist and under which unique IDs, and which registry entries are stale — no Home Assistant |
| `srtp.py` | SRTP (RFC 3711) encrypt/decrypt for the audio and video RTP streams |
| `media_handler.py` | RTP/SRTP transport for audio and video, H.264 depacketisation, the video registry, the AV ffmpeg process |
| `config_flow.py` | Config (manual or from zeroconf discovery), reconfigure and options flows — QR image upload or paste in, panel list and door command out |
| `camera.py` | Camera entity — the live stream and the keyframe-derived still |
| `event.py` | Doorbell event entity, also the source of the `vimar_intercom_ring` bus event |
| `lock.py` | Door locks: the generic one (the relay group from the QR) or the phonebook's door actuators |
| `button.py` | Call, door, answer, hang-up and reconnect buttons, and the phonebook's other actuators |
| `binary_sensor.py` | SIP registration and in-call sensors |
| `dashboard_card.py`, `frontend/vimar-intercom-card.js` | The dashboard card: served from a static path and added to every frontend page once per run, from `async_setup`. The card is one self-contained ES module with no build step; it reads the entities through their `intercom_role`/`panel` attributes (`const.py`) and acts only through the entities' own services |
| `const.py` | True constants — protocol values, config keys, defaults. Installation-specific values live in the config entry, not here |
| `manifest.json`, `strings.json`, `translations/` | Integration metadata and UI strings |

## Plant configuration

After every fresh registration the hub sends `GET_INIT_STATUS` (a SIP
MESSAGE with `Panda: blue`) to the indoor unit at `60001` — its address
on 2-wire V2, cloud-only and VGIP plants, per the SDK. The unit answers
with a MESSAGE of its own, `GET_INIT_STATUS_REPLY;[{"PARAM":…,"VALUE":…}]`,
naming the phonebook version (the MD5 of the file) and a download
token. If the version differs from the stored one, the phonebook is
fetched from `https://<cloud proxy>/phonebook/domains/<domain>/<version>`
with HTTP digest auth (the SIP domain and the token), checked against
its MD5, parsed in the executor, and saved with
`homeassistant.helpers.storage.Store` — without the token, which lives
in memory only and is never logged. A `NEW_PHONEBOOK;<version>;<gid>`
notification starts the same sync.

The entry is reloaded when the new configuration changes the entities,
not when only something else in the phonebook changed, and never during
a call: the hub waits for it to end. Reloading rather than adding and
removing entities in place keeps one derivation path — the stored
plant, the runtime config the SIP layer holds, the entity plan and the
registry cleanup are all built at setup, from the same plant, exactly
as after a restart. `entity_plan.py` decides the entities and the
unique IDs (a panel keeps `call_<ext>`/`door_<ext>`, the phonebook door
that is the old lock's door inherits `<entry>_lock`, other actuators are
`actuator_<target>_<command>`); setup removes the registry entries of
those families the plan no longer lists, before the platforms add
theirs. With no phonebook — no answer, no version, a failed download —
the options are used, exactly as before.

Actuators are triggered through `hub.async_door(target, command)`: a
MESSAGE to the actuator's GID with its command as the body and
`Panda: command`, which is the SDK's `sysMsgActuatorAction`.

Incoming MESSAGEs are answered with 200 OK first and then handed to the
hub whole; only the kinds of lines they held are logged. A MESSAGE with
a `Panda` family other than `blue` is not read as a notification.

## Connection state machine

```
disconnected → connecting → registering → registered → (call) → registered
      ^                                        |
      |                                        |
      └──────────── backoff, forever ──────────┘
```

Every edge back to `disconnected` — a TCP failure, a TLS failure, a
rejected REGISTER, or the server closing the connection — goes through
`connection_supervisor()`'s jittered exponential backoff
(`backoff.reconnect_delay`). There is no terminal failure state by
design: the supervisor retries forever, because the SIP connection is the
whole integration and giving up would mean the doorbell silently stops
working until Home Assistant is restarted. A connection that stays up for
at least `STABLE_CONNECTION_SECONDS` before failing again resets the
backoff ladder, so a momentary blip does not leave the next real outage
waiting at the ceiling delay.

Registration itself has its own lifetime: `is_registered()` reflects the
`expires` value the registrar actually granted, not just whether a
`200 OK` was ever seen, and a refresh is scheduled before that lifetime
runs out. A registration that stays down for more than five minutes
raises a Home Assistant repair issue; it clears itself automatically once
registration recovers.

## Locating the SIP server

The QR's `CPROXY` (`ipvdes.vimar.cloud` by default) names a SIP
domain, not a server, and nothing answers on it directly. Per RFC 3263,
every connection attempt looks up `_sips._tcp.<CPROXY>` SRV records
and tries the servers they name in RFC 2782 order: lowest priority
first, weighted random within a priority. Each server gets
`SIP_CONNECT_TIMEOUT` seconds for its TCP connect and TLS handshake;
a server that fails or stalls is logged and the next one tried, and
only when all have failed does the supervisor's backoff start. The
lookup is repeated on every reconnect, so a server change or a DNS
failover is picked up. With no SRV record, or a failed lookup, the
name itself is dialled on the configured port, which keeps a literal
host or IP address working.

Only the TCP destination changes. The TLS SNI and the certificate
hostname check, the `Route` header of every request and the SIP
domain keep using the names from the QR, so the certificate is
verified against the domain it is issued for rather than against
whichever server SRV picked.

The options flow's port is read as a deliberate override only when it
differs from the default 7042 (the flow always saves the field, so a
saved default cannot be told apart from a typed one); it then
replaces the port of every SRV server. `prefer_local` dials the
panel's LAN address directly and never goes through SRV.

## Transaction model

Responses are correlated to the request that caused them, not just
matched by method. The primary key is `branch|CSeq|method` — a fresh
branch on every retry means an authenticated retry is a distinct
transaction from the challenge that preceded it, so a stale or duplicate
response cannot be mistaken for the answer to the wrong request. The
Call-ID is kept as a fallback key for messages where the branch is not
authoritative. This replaces an earlier design that only checked the
method, which discarded a REGISTER reply that happened to arrive while a
call was active.

## Media pipeline

Audio and video arrive as SRTP over RTP/UDP once a call is established,
on separate ports (`RTPAudioProtocol`, `RTPVideoProtocol` in
`media_handler.py`):

- **Audio** is decrypted and the plain RTP forwarded to a local UDP port,
  where ffmpeg picks it up and remuxes it into the MPEG-TS served by the
  `/api/vimar_intercom/av` HTTP view. Nothing else consumes it: there is
  no decode to PCM and no buffer, because Home Assistant has no
  talk-back path and nothing ever read one. ffmpeg runs with `-c copy` —
  never `-c:v libx264` or any other transcode — because the reference
  deployment is a fanless two-core machine that a live re-encode would
  saturate.
- **Video** is decrypted, depacketised from RTP H.264 (FU-A and STAP-A)
  into Annex-B NAL units, and handed to `VideoStreamRegistry`
  (`media_handler.py`), which fans them out to consumers. It caches the
  most recent SPS/PPS and replays them ahead of every IDR — and to any
  new consumer as soon as it attaches — so a decoder that starts
  mid-stream, or loses sync, can still recover. A consumer whose write
  stalls transiently gets a small backlog (capped at 256 KB) to catch up
  from; one that cannot recover, or was never seen again, is dropped.

The one consumer wired up today is ffmpeg's stdin: the
`/api/vimar_intercom/av` HTTP view starts ffmpeg when the first viewer
attaches, which remuxes (`-c copy`, never a transcode) the H.264
arriving on stdin with the PCMU audio arriving over a local RTP port
into MPEG-TS on stdout. The pipeline is **reference counted** and its
output is **fanned out**: one reader task reads ffmpeg's stdout and
pushes each chunk to a queue per viewer, so a second dashboard joins the
running pipeline instead of restarting it, and the last viewer to leave
is the one that stops it. The camera entity exposes the view through a
**signed path** — `async_sign_path` from
`homeassistant.components.http.auth` — because Home Assistant's `stream`
component fetches `stream_source()` without carrying a bearer token, and
the view still sets `requires_auth = True`. The signature outlives the
maximum call duration, so a stream cannot outlive its own URL.

The registry also caches the most recent SPS + PPS + IDR as a
self-contained Annex-B keyframe. That is what a **still** is decoded
from: `async_camera_image` turns it into one JPEG with a single ffmpeg
invocation while a call is running, and returns `None` otherwise.
`use_stream_for_stills` is deliberately False — taking a still through
the stream would fetch the AV view, and the AV view places a SIP call to
the entrance panel, so a dashboard polling the still would ring the door
every few seconds.

**This is built and unit tested but not verified against a real panel.**
Whether the `stream` component's ffmpeg opens a signed internal URL
cleanly, and whether the MPEG-TS the panel produces decodes without
artifacts, is behavioural and needs a live Vimar panel — see the
README's note on why that validation is a scheduled session, not
something to try casually.

## Dashboard card

`async_setup` registers `frontend/vimar-intercom-card.js` as a static
path and adds it to every frontend page with `add_extra_js_url`, with
the manifest version as a `?v=` cache buster. It runs once per Home
Assistant run, not per entry reload, and a failure is logged rather
than raised: the doorbell must not depend on a card.

The card is a plain `HTMLElement` with a shadow root. Home Assistant
does not export Lit, and borrowing it through an internal element's
prototype breaks the day the frontend's bundling changes. Each section
is rendered to a string and replaced only when the string changes, so
the stream element (`ha-camera-stream`, the frontend's own HLS/WebRTC
player) is created once per viewing and only has its `hass` updated.

It finds its entities in `hass.entities` (platform, device, translation
key) and tells them apart by the `intercom_role` attribute every
entity carries, and the `panel` attribute of the call and open buttons;
the camera's `default_panel` says which panel watching calls on its
own. It calls nothing but the entities' services (`button.press`,
`lock.unlock`), so it can do nothing an automation could not.

Two choices follow from the hub rather than from taste. A ring does
not start the video: opening the stream while a panel rings answers it
(`_do_auto_call`), so a card that auto-played would pick up every
visitor on every open dashboard. And the red button while ringing is
Dismiss, which only hides the banner: there is no decline entity, Hang
up does not refuse a pending INVITE, and a 603 would stop the rest of
the house ringing. The frontend is told when a ring starts (the event
entity's state) but not when it is cancelled, so the banner expires
after 30 seconds, or as soon as `in_call` turns on.

## Threat model

- The config entry holds the SIP account credentials in Home Assistant's
  own config entry store; anyone with access to `.storage` on the host
  has them, the same as for any other integration's credentials.
- Every `HomeAssistantView` this integration registers sets
  `requires_auth = True`, with no exception — the door release is
  reachable through the SIP stack these views front, so an
  unauthenticated view would let anyone on the network that can reach
  Home Assistant open the door. The AV stream view is the one client
  that cannot present a bearer token — Home Assistant's `stream`
  component fetches `stream_source()` directly — so the camera signs
  that URL with `async_sign_path` instead of disabling auth; the view
  itself is unchanged and still requires it. The signature's lifetime is
  derived from `MAX_CALL_DURATION`, so it always outlives the longest
  call the integration allows — a stream cannot be cut by its own URL
  expiring and then fail to restart on a 401 it could not recover from.
- The SIP connection to the cloud proxy is TLS with the certificate
  verified, and the integration refuses to set up at all when
  `vimar_rootca.pem` is missing: it used to fall back to an unverified
  connection, silently, on the socket that carries the door command and
  the digest response. Verification is *not* certificate pinning.
  `load_verify_locations` on top of `ssl.create_default_context()` adds
  the Vimar CA to the system roots rather than replacing them, so any
  publicly trusted certificate for the proxy host also validates. That
  is deliberate: pinning cannot be tested here, it would break every
  installation the day Vimar rotates to a different chain, and
  `prefer_local` — which connects to a panel's LAN address while passing
  the cloud proxy as the SNI — almost certainly could not satisfy it.
- Digest authentication answers whatever realm the server names, rather
  than requiring it to equal the configured SIP domain. A proxy can
  therefore choose a realm and collect `MD5(user:realm:password)`, which
  is offline-crackable; the password itself never leaves the host.
  Reaching that position means being the real proxy or holding a
  certificate the system roots trust for it. Requiring the realm to
  match the domain would close it, and would also break any installation
  whose registrar names a realm that is not its domain — which cannot be
  checked from here. Left open deliberately, to be settled during live
  validation.
- The integration never opens an inbound port on the internet. It
  maintains one outbound TLS connection to the Vimar cloud proxy. It
  does bind two UDP sockets on `0.0.0.0` for the RTP media streams
  (`rtp_port_base` and `rtp_port_base + 2000`), reachable from the local
  network: `datagram_received` does not check the source address, so
  while SRTP is negotiated a forged datagram fails authentication and is
  counted, but a panel that offers no crypto leaves the stream in
  plaintext and any host on the LAN could inject frames into the video
  the camera shows. No inbound TCP port is opened, and nothing from
  outside the local network can reach either socket.
- The dashboard card's static path is served without authentication,
  as every frontend file is. It holds the card's code and nothing
  else: no entity, no name, no address. The card itself acts only
  through entity services, under the logged-in user's own permissions.
- The AV stream view is reachable by any authenticated Home Assistant
  user, including non-admins and users for whom the camera entity is
  hidden: entity permissions do not apply to a `HomeAssistantView`. That
  is how Home Assistant works rather than something introduced here, but
  fetching the view places a call to the entrance panel, so it is worth
  knowing about.
- The phonebook download is authenticated with a token the indoor unit
  hands out in its status reply. It is kept in memory only, never
  persisted and never logged; a Basic challenge is refused rather than
  answered with it, and redirects are not followed. The downloaded
  phonebook is accepted only if its MD5 is the version the indoor unit
  announced, and every extension and command read from it — or from
  the stored copy — is checked against the same strict patterns as the
  QR fields before it can reach a SIP URI or a MESSAGE body. Only this
  apartment's own group is kept from the phonebook, not the names of
  the neighbouring flats a block's phonebook lists.
- The QR payload and the SIP password are excluded from logging by
  design (see `qr.py`); no module sets a logging level or attaches a
  handler — Home Assistant's own `logger:` configuration is the only
  authority on verbosity. No log statement lives on a path executed per
  packet or per NAL, at any level: the RTP receive paths count what they
  see and `stop_media` emits a single DEBUG summary per call.

## Known compatibility

Developed against a Vimar Elvox Tab 5S Plus (40515/40517) on a 2-wire Due
Fili Plus system. The protocol dialect is shared across Vimar/Elvox SIP
video door entry panels that pair with the Vimar View app, so other
models are likely to work, but none have been verified.
