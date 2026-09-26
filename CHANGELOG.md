# Changelog

## [2.1.1] — 2026-09-26

### vimar_intercom

#### Added
- **Audio listeners**: `media_handler.add_audio_listener(cb)` and
  `remove_audio_listener(cb)` let another component listen to the
  panel's audio during a call. Each listener gets the decrypted PCMU
  payload of every packet (G.711 µ-law, 8 kHz mono, normally 20 ms).
  A listener that raises is counted in the per-call media summary
  (`listener_errors`) and skipped; the others and the AV stream carry
  on, and nothing is logged per packet.

## [2.1.0] — 2026-09-26

### vimar_intercom — native-app parity

#### Added
- **Do not disturb** and **answering machine** switches, following the
  indoor unit (its status reply and its notifications) and switching it
  through the apartment intercom address from the phonebook, as the
  Vimar View app does.
- **Answering-machine delay** select, with the choices the unit offers;
  a change is adopted only when the unit confirms it, and a refusal is
  raised with the unit's error code.
- **Video messages**: the unit's mailbox is read after every
  registration and on every change notice; a sensor counts the unread
  messages and lists them; services play, mark read, delete and delete
  all; `vimar_intercom_video_message` fires for a new one. Playback is a
  call to the message's extension, shown on the camera like any call.
- **Mailbox usage** sensor, from the unit's `vm_level`.
- **Missed calls** sensor with the last 20 rings and how each ended
  (answered here, answered elsewhere, declined, missed), stored across
  restarts; `vimar_intercom_missed_call` fires once per missed visitor;
  `vimar_intercom.clear_missed_calls` resets the count.
- **Ringing** binary sensor, on from the INVITE until the call is
  answered, declined, cancelled or answered on another device.
- **Decline** button (available while ringing) and **Next / Previous
  camera** buttons (available during a call from a panel with several
  cameras).
- Answering a call now tells the other devices of the apartment
  (`C;<call id>;ANSWERED`), as the app does.
- A diagnostics download, redacted of every credential and identity.
- The dashboard card uses all of it: Decline in the ringing banner with
  "Silence here" beside it, the banner driven by the Ringing sensor
  instead of a 30-second guess, do-not-disturb and answering-machine
  toggles, video-message and missed-call lists, and camera switching
  during a call.
- **Talk-back from the dashboard card.** While a call is being watched
  in the card, **Hold to talk** plays the browser's microphone out of
  the entrance panel: hold while speaking, or tap to keep talking and
  tap again to stop. The audio travels over the frontend's existing
  websocket through a binary handler (`vimar_intercom/talk`, the
  mechanism Assist uses), as 8 kHz 16-bit PCM the card resamples to,
  and is encoded to G.711 µ-law in pure Python and sent in the call's
  SRTP audio stream in place of the silence, with a 200 ms jitter
  buffer. One person talks at a time, the newest taking over. The
  stream's sound is muted while talking, so your own voice does not
  come back from the panel seconds later. Needs HTTPS and microphone
  permission; over plain HTTP the card says so.
- The mailbox table is found whatever the case of its name: the indoor
  unit names it in lower case.

#### Changed
- The SIP reader accepts MESSAGE bodies up to 3 MB (was 128 KB): the
  unit sends its mailbox as one base64 SQLite file.
- A system message that cannot be sent is logged with its kind only,
  never its body.

## [2.0.0] — 2026-09-26

### vimar_intercom v2.0.0

**Breaking:** credentials no longer live in `const.py`. Upgrading from 1.x
requires removing the existing entry and adding the integration again
with the QR code the indoor unit generates. Nothing needs to be edited
by hand any more.

#### Added
- QR-based config flow, with reconfigure and options flows.
- **Setup and reconfigure accept a photo or screenshot of the QR code**,
  as well as its text. The indoor unit only shows the QR on screen, so
  asking for pasted text left most people stuck. The image is read with
  `pyzbar`, which is not a new dependency: Home Assistant core's own
  `qrcode` integration requires `pyzbar==0.1.9` and `Pillow`, and the
  official image ships both with the native `libzbar`. The manifest
  declares `pyzbar>=0.1.9`, a lower bound so it can never conflict with
  core's pin, and `file_upload` as a dependency. The upload is deleted
  as soon as it has been read. If a Home Assistant Core install lacks
  `libzbar`, the form says so and the text paste still works. HEIC
  photos are refused with a request for a screenshot or a JPEG, since
  nothing in Home Assistant can open them; an image holding two
  different QR codes is refused rather than guessed at.
- **Entrance panels, door locks and actuators are discovered from the
  plant's phonebook**, with the names set on the indoor unit, the way
  the Vimar View app shows them. After registering, the integration asks
  the indoor unit for its status, downloads the phonebook from the Vimar
  cloud when its version changed, keeps the last good copy, rebuilds the
  entities when the installer changes the plant (never during a call)
  and removes the ones that are gone. Existing panel buttons and the
  door lock keep their unique IDs. Without a phonebook the options are
  used as before.
- **The indoor unit is discovered on the local network** (it announces
  itself over mDNS as `_eipvdes._tcp`), so Home Assistant offers to set
  it up. The QR code is still what supplies the credentials; a QR from a
  different unit than the one discovered is refused.
- **A dashboard card ships with the integration** (`custom:vimar-intercom-card`):
  live video with a panel selector, a ringing banner with Answer,
  Dismiss and Open door, door buttons that need a second tap or a hold,
  the other actuators, and the registration state with Reconnect. It is
  served and loaded on every dashboard by the integration itself, so
  there is no resource to add; it appears in the card picker and has a
  visual editor. English and Italian, light and dark themes. A ring
  does not start the video on its own, because opening the stream while
  a panel rings answers the call. The integration now declares
  `frontend` as a dependency.
- Every entity carries an `intercom_role` attribute, the call and open
  buttons `panel` and `panel_name`, and the camera `default_panel` and
  `default_panel_name`, so a card can tell them apart without guessing
  from names.
- `vimar_intercom_ring` event on the Home Assistant bus, carrying the
  panel address and name.
- Reconnect button and a repair issue raised when registration has been
  down for more than five minutes.
- Configurable panel addresses; one lock and one call button per panel.
- Unit tests for QR decryption, configuration derivation, SIP
  transaction correlation, the reconnect schedule and SPS/PPS replay,
  running in CI.

#### Fixed
- **SIP registration never came up: the cloud proxy name was dialled
  as if it were a server.** The QR's `CPROXY`, `ipvdes.vimar.cloud`, is
  a SIP domain whose `_sips._tcp` SRV records name the servers that
  actually listen; a TCP connection to the name itself times out. 1.x
  worked because it hardcoded one of those servers; the rewrite lost
  that. The integration now locates the server the RFC 3263 way, as the
  vendor's app does: it looks up the SRV records on every connection
  attempt, tries the servers in RFC 2782 order (priority, then weighted
  random) and moves to the next one when a server fails. Without SRV
  records the name is dialled directly, so a literal host or IP still
  works. The TLS SNI, the certificate hostname check, the `Route`
  header and the SIP domain still use the names from the QR; only the
  TCP destination changed. A cloud SIP port set in the options to
  anything other than 7042 overrides the SRV port. The local panel
  path is unchanged. The lookup uses `aiodns`, which Home Assistant
  core already requires; the manifest gains no requirement.
- **A dead SIP server stalled the connection silently.** Connecting had
  no timeout, so a blackholed host left the log at "Connecting to the
  SIP proxy" with no error and no retry. Each server now gets 15
  seconds for its TCP connect and TLS handshake; a failure is logged
  with its cause at WARNING, the next server is tried, and after the
  last one the usual backoff applies. The server chosen is logged at
  INFO.
- **A phone photo of the indoor unit's screen was usually not read.**
  The decoder made a single zbar pass over the full 12 MP frame, and a
  photo of a backlit LCD (moire from the subpixel grid, glare, noise, a
  slight angle, a dense code in a corner of the frame) defeats that about
  two times in three. It now tries the image several ways, cheapest
  first, stopping at the first success: the image as uploaded, rescaled
  copies, a local-mean (adaptive) threshold, the QR region located and
  straightened out of its perspective, and an inverted threshold for a
  code drawn light on dark. The search is bounded to 12 attempts and 3
  seconds. On synthetic phone photos of an LCD it reads about 98% where
  the single pass read about 33%. Pillow and numpy only; no new
  requirement.
- **The setup instructions pointed to a menu that does not exist.** They
  sent users to "Settings → System → Export configuration" in the Vimar
  View app, which only scans QR codes. The QR is generated on the indoor
  unit, under Settings → Network and devices → Mobile device pairing
  (one slot per paired device, up to ten), and the dialog, the repair
  issue and the README now say so, recommending a slot dedicated to
  Home Assistant.
- Reconnection is unbounded with jittered exponential backoff. Previously
  the client gave up after five attempts and stayed silent until Home
  Assistant restarted.
- SIP responses are correlated per transaction, so a REGISTER reply
  arriving during a call is no longer discarded as stale.
- A camera stream that never connected, or a call the panel ended
  itself, no longer leaves the integration suppressing every subsequent
  doorbell press. The record of a locally placed call now has one owner
  and is cleared on every path out of a call.
- The SIP layer's state is reset when the integration starts and a live
  call is hung up when it stops, so reloading the entry mid-call no
  longer leaves the in-call sensor stuck on and the camera unable to
  place another call.
- The inbound SIP request loop survives a failure while answering a
  request. It used to die silently on a dropped connection, leaving the
  connectivity sensor green and the doorbell permanently deaf.
- A second viewer joins the running AV pipeline instead of destroying
  the first viewer's ffmpeg and splitting the transport stream.
- The AV HTTP view is registered once rather than once per setup, so an
  options change no longer leaves camera opens routed through a
  torn-down hub, firing a spurious `vimar_intercom_ring` each time.
- Opening the door while the cloud is unreachable fails fast with an
  explanation and asks for a reconnect, instead of leaking one TLS
  socket per attempt.
- The RTP receive paths no longer log per packet or per NAL. A few
  percent of packet loss on residential Wi-Fi used to produce thousands
  of WARNING lines per call; the media layer now emits one DEBUG
  summary when the call ends.
- The RTP sockets are all closed on unload, and a failed bind during
  setup cleans up after itself and surfaces as a retryable setup error.
- Registration state follows the lifetime granted by the registrar, with
  a refresh at half that lifetime.
- Video depacketisation holds the most recent SPS/PPS and replays them
  to every new consumer and ahead of every IDR, so a consumer attaching
  mid-stream, or recovering from lost sync, can still decode. A
  transiently stalling consumer gets a small bounded backlog instead of
  being dropped outright.
- The camera streams the panel's video and audio: ffmpeg copies the
  depacketised H.264 and encodes the panel's G.711 audio to AAC into
  MPEG-TS, and the camera entity fetches it over a signed URL, so the
  underlying HTTP view still requires authentication.
- **The stream carried no frames.** Raw H.264 on ffmpeg's stdin has no
  timestamps, and the MPEG-TS muxer refused every packet: a viewer got
  the stream headers and nothing else. Packets are now stamped with
  their arrival time, and ffmpeg probes for half a second instead of
  five, so video starts within the auto-on call.
- **The stream was silent in browsers.** G.711 in MPEG-TS is an opaque
  data stream no player decodes; the audio is now AAC.
- **Auto-on views were cut to eight seconds.** A call carrying no media
  from this side was ended by the far end after about ten seconds. The
  client now sends muted-microphone silence for the length of the call.

#### Changed
- **A camera snapshot no longer places a call.** Stills used to be taken
  by building a stream, which fetched the AV view, which called the
  entrance panel: a dashboard card polling the still every ten seconds
  occupied the household's intercom for as long as it was open. A still
  is now decoded from the keyframe of a call already in progress, and
  outside a call the camera honestly returns no image.
- **Opening the camera while a panel is ringing answers that call**
  instead of placing a second, colliding one to the default panel.
- **The maximum call duration (five minutes) is documented.** A call
  this integration placed or answered is hung up after five minutes as
  a safety net.
- **Upgrading from 1.x now explains itself.** A 1.x config entry raises
  a repair issue naming the remove-and-re-add step instead of failing
  with "Migration handler not found for entry".

#### Known limitations
- There is no still image outside a call.
- Audio flows from the panel only. There is no talk-back.
- One Vimar system per Home Assistant installation.
- The AV stream view is reachable by any authenticated Home Assistant
  user; entity permissions do not apply to an HTTP view.

#### Security
- Every HTTP view requires authentication. The audio WebSocket, MJPEG
  and AV views previously did not, which let anyone on the network open
  the street gate.

#### Removed
- Apple push (APNs/PushKit) support and the `/api/vimar_intercom/push_token`
  endpoint. The integration stops at the `vimar_intercom_ring` event;
  subscribe to it from a notification service or a companion app.
- `connectProfiles` registration against the Vimar cloud. It sent a
  locally generated random push token — and used that same invented
  token as the digest password — to register a push channel this
  integration has no way to receive. It could not succeed by
  construction, and it left the SIP socket unread for up to 45 seconds
  after every reconnect.
- The audio WebSocket, MJPEG and debug endpoints.
- The import-time debug log handler that forced every installation to
  DEBUG.
- The unused audio decode buffer, the unused RTP send path, and the
  unused panel probe and scan helpers.

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
