# Changelog

## [2.0.0] — 2026-09-10

### vimar_intercom v2.0.0

**Breaking:** credentials no longer live in `const.py`. Upgrading from 1.x
requires removing the existing entry and adding the integration again,
pasting the QR payload from the Vimar View app. Nothing needs to be
edited by hand any more.

#### Added
- QR-based config flow, with reconfigure and options flows.
- `vimar_intercom_ring` event on the Home Assistant bus, carrying the
  panel address and name.
- Reconnect button and a repair issue raised when registration has been
  down for more than five minutes.
- Configurable panel addresses; one lock and one call button per panel.
- Unit tests for QR decryption, configuration derivation, SIP
  transaction correlation, the reconnect schedule and SPS/PPS replay,
  running in CI.

#### Fixed
- Reconnection is unbounded with jittered exponential backoff. Previously
  the client gave up after five attempts and stayed silent until Home
  Assistant restarted.
- SIP responses are correlated per transaction, so a REGISTER reply
  arriving during a call is no longer discarded as stale.
- Registration state follows the lifetime granted by the registrar, with
  a refresh at half that lifetime.
- Video depacketisation holds the most recent SPS/PPS and replays them
  to every new consumer and ahead of every IDR, so a consumer attaching
  mid-stream, or recovering from lost sync, can still decode. A
  transiently stalling consumer gets a small bounded backlog instead of
  being dropped outright.
- The camera streams the panel's video and audio: ffmpeg remuxes
  (`-c copy`) the depacketised H.264 and the RTP audio into MPEG-TS, and
  the camera entity fetches it over a signed URL, so the underlying HTTP
  view still requires authentication.

#### Known limitations
- The camera is implemented but not yet verified end to end against a
  live panel: it is built and unit tested, but a second SIP registration
  would deregister the production panel, so real-hardware validation is
  a scheduled session, not something to try casually.
- Audio flows from the panel only. There is no talk-back.
- One Vimar system per Home Assistant installation.

#### Security
- Every HTTP view requires authentication. The audio WebSocket, MJPEG
  and AV views previously did not, which let anyone on the network open
  the street gate.

#### Removed
- Apple push (APNs/PushKit) support and the `/api/vimar_intercom/push_token`
  endpoint. The integration stops at the `vimar_intercom_ring` event;
  subscribe to it from a notification service or a companion app.
- The audio WebSocket, MJPEG and debug endpoints.
- The import-time debug log handler that forced every installation to
  DEBUG.

## [1.1.0] — 2026-03-12

### dreame_h15pro v2.0.0

- Initial public release (ported from private HA install)
- Vacuum, sensors, switches, selects, number, binary sensors
- Dreame Cloud API with OAuth token auth

### madoka_energy v0.1.0

- Initial public release
- BLE polling for energy consumption (today/yesterday/week/year)
- Bluetooth auto-discovery + manual MAC entry
- Energy Dashboard compatible

### vimar_intercom v1.3.0

- Initial public release
- SIP stack integration for Vimar Elvox panels
- Camera, event, lock, button, binary_sensor entities
- Local push, no cloud dependency

### universal_audio v1.0.0

- Initial public release
- UA Console TCP protocol integration
- media_player, monitor volume, phantom power, Hi-Z, phase, sample rate

---

## [1.0.0] — 2026-03-12

### octopus_energy_italy

#### Added
- Initial release
- Authentication via Kraken GraphQL API (`api.oeit-kraken.energy`) with JWT + auto-refresh
- Electricity consumption sensors: yesterday, this month, this year (kWh)
- Gas consumption sensors: this month, this year (Smc)
- Tariff sensors: electricity rate (€/kWh), gas rate (€/Smc), standing charges
- Account balance sensor (€)
- Config flow UI: email + password → auto-discover accounts and supply points
- Italian and English translations
- Energy Dashboard compatible (`state_class: total_increasing`)
- HA device grouping — all sensors under one device per account
