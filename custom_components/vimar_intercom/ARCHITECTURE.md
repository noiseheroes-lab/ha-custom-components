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
| `media_handler.py` | RTP/SRTP transport for audio and video, H.264 depacketisation, the video registry, the AV ffmpeg process |
| `config_flow.py` | Config, reconfigure and options flows — QR paste in, panel list and door command out |
| `camera.py` | Camera entity — the live stream and the keyframe-derived still |
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
- The AV stream view is reachable by any authenticated Home Assistant
  user, including non-admins and users for whom the camera entity is
  hidden: entity permissions do not apply to a `HomeAssistantView`. That
  is how Home Assistant works rather than something introduced here, but
  fetching the view places a call to the entrance panel, so it is worth
  knowing about.
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
