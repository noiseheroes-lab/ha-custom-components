# Vimar Intercom — Architecture

## Overview

This integration speaks the SIP dialect the Vimar cloud uses for the
Vimar View app. It is not a generic SIP client for local Vimar/Elvox
panels: the panel is reached through Vimar's own cloud proxy
(`ipvdes.vimar.cloud:7042` by default), the same path the phone app uses,
using a user agent string and REGISTER/INVITE shape the cloud is known to
accept. This dialect is not documented by Vimar; it was reverse
engineered from the app and from captured traffic. It works today and it
can break the day Vimar changes something on their end.

## Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | Entry setup/teardown, wires the hub to Home Assistant, registers the AV stream HTTP view |
| `hub.py` | Orchestrates SIP registration, calls, door control and media lifecycle; the only module that talks to both `sip_client` and Home Assistant |
| `sip_client.py` | The SIP stack itself: connection, digest auth, REGISTER/INVITE/BYE/MESSAGE, transaction correlation, reconnection |
| `sip_parser.py` | Pure text handling: header parsing, transaction keys, registration expiry parsing — no I/O, no Home Assistant |
| `backoff.py` | The jittered exponential reconnect delay schedule |
| `qr.py` | Decrypts and parses the QR configuration payload exported by the Vimar View app |
| `runtime.py` | `RuntimeConfig` — every value the integration needs, derived once from the config entry; no Home Assistant import, fully unit testable |
| `srtp.py` | SRTP (RFC 3711) encrypt/decrypt for the audio and video RTP streams |
| `media_handler.py` | RTP/SRTP transport for audio and video, G.711 decoding, H.264 depacketisation, the AV ffmpeg process |
| `config_flow.py` | Config, reconfigure and options flows — QR paste in, panel list and door command out |
| `camera.py` | Camera entity |
| `event.py` | Doorbell event entity, also the source of the `vimar_intercom_ring` bus event |
| `lock.py` | Door lock entity (opens the relay group from the QR) |
| `button.py` | Call, door, answer, hang-up and reconnect buttons |
| `binary_sensor.py` | SIP registration and in-call sensors |
| `const.py` | True constants — protocol values, config keys, defaults. Installation-specific values live in the config entry, not here |
| `manifest.json`, `strings.json`, `translations/` | Integration metadata and UI strings |

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

- **Audio** is decrypted, decoded from G.711 μ-law, and buffered for
  internal use; the decrypted RTP is also forwarded locally so an ffmpeg
  process can remux it into MPEG-TS for the `/api/vimar_intercom/av` HTTP
  view. ffmpeg runs with `-c copy` — never `-c:v libx264` or any other
  transcode — because the reference deployment is a fanless two-core
  machine that a live re-encode would saturate.
- **Video** is decrypted, depacketised from RTP H.264 (FU-A and STAP-A)
  into Annex-B NAL units, and handed to `VideoStreamRegistry`
  (`media_handler.py`), which fans them out to consumers. It caches the
  most recent SPS/PPS and replays them ahead of every IDR — and to any
  new consumer as soon as it attaches — so a decoder that starts
  mid-stream, or loses sync, can still recover. A consumer whose write
  stalls transiently gets a small backlog (capped at 256 KB) to catch up
  from; one that cannot recover, or was never seen again, is dropped.

The one consumer wired up today is ffmpeg's stdin: the `/api/vimar_intercom/av`
HTTP view starts ffmpeg when it is first opened, which remuxes (`-c
copy`, never a transcode) the H.264 arriving on stdin with the PCMU audio
arriving over a local RTP port into MPEG-TS on stdout. The camera entity
exposes that view through a **signed path** — `async_sign_path` from
`homeassistant.components.http.auth`, ten-minute expiry — because Home
Assistant's `stream` component fetches `stream_source()` without
carrying a bearer token, and the view still sets `requires_auth = True`.

**This is built and unit tested but not verified against a real panel.**
Whether the `stream` component's ffmpeg opens a signed internal URL
cleanly, and whether the MPEG-TS the panel produces decodes without
artifacts, is behavioural and needs a live Vimar panel — see the
README's note on why that validation is a scheduled session, not
something to try casually.

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
  that URL with `async_sign_path` (ten-minute expiry) instead of
  disabling auth; the view itself is unchanged and still requires it.
- The integration never opens an inbound port on the internet. It
  maintains one outbound TLS connection to the Vimar cloud proxy; nothing
  listens for connections from outside the local network.
- The QR payload and the SIP password are excluded from logging by
  design (see `qr.py`); no module sets a logging level or attaches a
  handler — Home Assistant's own `logger:` configuration is the only
  authority on verbosity.

## Known compatibility

Tested against a Vimar Elvox Tab 5S Plus (40515/40517) on a 2-wire Due
Fili Plus system. The protocol dialect is shared across Vimar/Elvox SIP
video door entry panels that pair with the Vimar View app, so other
models are likely to work, but none have been verified.
