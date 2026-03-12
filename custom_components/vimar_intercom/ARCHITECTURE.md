# Vimar Intercom — Architecture

## Overview

This integration connects Home Assistant to a **Vimar Elvox** video intercom panel using the **SIP** (Session Initiation Protocol, RFC 3261) and **RTSP** (Real Time Streaming Protocol, RFC 2326) standard protocols.

No cloud dependency. No reverse engineering of proprietary protocols — Vimar panels expose SIP natively and document the RTSP stream endpoint in their installation manual.

---

## File structure

```text
vimar_intercom/
├── __init__.py           Entry setup / teardown, SIP stack lifecycle
├── sip_client.py         SIP stack (registration, call handling, door unlock)
├── config_flow.py        UI config flow (SIP credentials entry)
├── camera.py             Camera entity (RTSP stream via ffmpeg)
├── event.py              Event entity (doorbell press)
├── lock.py               Lock entity (door/gate opener)
├── button.py             Button entities (call, hangup)
├── binary_sensor.py      Binary sensors (SIP registered, in-call)
├── const.py              SIP message templates, default ports
├── manifest.json
├── strings.json
├── translations/en.json
├── README.md
├── ARCHITECTURE.md       This file
└── icon.svg
```

---

## Protocol overview

### SIP (RFC 3261)

SIP is a signalling protocol used for establishing, managing, and terminating multimedia sessions (calls). Vimar/Elvox intercoms act as SIP endpoints on the local network.

This integration acts as a **SIP User Agent** (UA) that:

1. **Registers** with the intercom panel as an extension (`REGISTER`)
2. **Receives** incoming `INVITE` from the panel when the doorbell is pressed
3. **Sends** `MESSAGE` to the panel to trigger door unlock
4. **Sends** outbound `INVITE` to call the panel
5. **Sends** `BYE` to hang up

The SIP stack runs as a background asyncio task, maintaining registration with periodic `REGISTER` refreshes (every 60–120s, per the panel's `expires` parameter).

### RTSP (RFC 2326)

The intercom panel streams live video over RTSP. HA's `camera` platform wraps this as an FFmpeg-proxied camera entity:

```
rtsp://<panel_ip>:<port>/stream
```

The RTSP URL is configurable in the config flow (default follows Elvox Tab 5S Plus documentation).

---

## SIP stack implementation

The integration implements a minimal SIP stack in Python using asyncio — no external SIP library dependency.

### Registration

```
REGISTER sip:<panel_ip> SIP/2.0
From: sip:<extension>@<panel_ip>
To: sip:<extension>@<panel_ip>
Contact: sip:<extension>@<ha_ip>:<sip_port>
Expires: 120
Authorization: Digest ...
```

Registration is renewed before expiry. The `binary_sensor.sip_registered` entity reflects the current registration state.

### Doorbell event (incoming INVITE)

When the doorbell is pressed, the panel sends a SIP `INVITE` to the registered HA extension:

```
INVITE sip:<ha_extension>@<ha_ip> SIP/2.0
From: sip:<panel_extension>@<panel_ip>
...
```

The integration:

1. Sends `200 OK` (auto-answers at SIP level — no audio negotiation)
2. Fires HA event `vimar_intercom_campanello`
3. Sets `binary_sensor.in_call = True`
4. Sets `event.campanello` state

Automations can respond (e.g., send a push notification, trigger a camera view).

### Door unlock (outbound MESSAGE)

```
MESSAGE sip:<door_extension>@<panel_ip> SIP/2.0
Content-Type: application/dtmf-relay
Signal=*
Duration=250
```

The specific signal/duration to trigger the door relay is configurable and panel-dependent. The Elvox Tab 5S Plus uses a DTMF relay via `*`.

### Call / Hangup

- **Call**: sends `INVITE` to the panel extension — triggers the panel's screen and audio
- **Hangup**: sends `BYE` to terminate the active dialog

---

## Config flow

```
Step 1: user_input(panel_ip, sip_extension, sip_password, rtsp_url)
  → attempt SIP REGISTER to validate credentials
  → if 200 OK: create entry
  → if 401/403: show authentication error
  → if timeout: show cannot_connect error
```

No multi-step flow needed — all config is entered at once. The config is stored in the entry as:

```python
{
    "host": "192.168.1.x",
    "sip_extension": "55001",
    "sip_password": "...",
    "sip_port": 5060,
    "rtsp_url": "rtsp://192.168.1.x:554/stream",
    "door_extension": "55001"
}
```

---

## Entity model

| Entity | HA type | Update mechanism |
| ------ | ------- | ---------------- |
| Camera | `camera` | Passive RTSP stream (always on) |
| Doorbell | `event` | Fired on incoming INVITE |
| Lock | `lock` | Write-only (open = SIP MESSAGE), no state feedback |
| Call button | `button` | Sends outbound INVITE |
| Hangup button | `button` | Sends BYE |
| SIP Registered | `binary_sensor` | SIP registration state machine |
| In Call | `binary_sensor` | SIP dialog state machine |

The lock entity has no physical feedback — it always shows as "unlocked" after the open action (the panel has no state reply for the door sensor). If door sensor state is needed, a separate `binary_sensor` connected to a door contact sensor would be required.

---

## Notes on SIP compatibility

The integration has been tested with the **Elvox Tab 5S Plus** (also sold as Vimar K40945). Other Vimar/Elvox SIP-based panels should work if they implement standard SIP REGISTER + INVITE + MESSAGE. The following are known working:

- Elvox Tab 5S Plus
- Vimar VIEW IP series (2-wire SIP gateway)

For panels with non-standard SIP behaviour, the `sip_client.py` can be extended.

---

## Legal and protocol notes

SIP is an open IETF standard (RFC 3261). RTSP is an open IETF standard (RFC 2326). Vimar panels expose SIP as a documented feature. There is no proprietary protocol reverse engineering, no DRM circumvention, and no security bypass in this integration. Users provide their own device credentials. This integration is equivalent in nature to any other SIP client (softphone, door intercom app).

---

## Future improvements

- QR code scan in config flow to auto-fill SIP credentials (Elvox QR codes encode SIP config)
- Two-way audio via RTP (requires audio codec negotiation in SIP)
- Multi-panel support in a single entry (esterna + interna)
- DTMF tones for gate automation sequences
- Auto-answer mode with notification-triggered accept/decline
