# Universal Audio Apollo — Architecture

## Overview

This integration controls and monitors **Universal Audio Apollo** audio interfaces via the **UA Console** application's TCP control protocol. UA Console runs on macOS and exposes a local TCP socket for remote control — this is the same channel used by UA's own iOS remote app.

---

## File structure

```text
universal_audio/
├── __init__.py           Entry setup / teardown
├── tcp_client.py         UA Console TCP protocol client
├── config_flow.py        UI config flow (host, port, device index)
├── coordinator.py        State manager (persistent TCP connection + push updates)
├── media_player.py       Media player entity (volume, mute, source)
├── number.py             Number entities (monitor level, input gain)
├── sensor.py             Sensor entity (sample rate)
├── switch.py             Switch entities (phantom power, Hi-Z, phase per channel)
├── const.py              Protocol constants, message types
├── manifest.json
├── strings.json
├── translations/en.json
├── README.md
├── ARCHITECTURE.md       This file
└── icon.svg
```

---

## UA Console TCP protocol

UA Console listens on TCP port **4710** (default, configurable). The protocol is a binary framed message format used by UA's own remote control apps.

### Connection lifecycle

```
1. TCP connect to <host>:<port>
2. Send handshake / subscription message
3. UA Console sends full state snapshot
4. UA Console sends incremental updates as state changes (push)
5. Client sends commands as needed
6. Keepalive / heartbeat to detect disconnects
```

This is a **persistent connection** with push-based state updates — not polling. The coordinator maintains one TCP connection per ConfigEntry and processes incoming messages on a background asyncio task.

### Message format

Messages are framed with a 4-byte length header followed by a JSON or binary payload (UA Console uses a proprietary encoding for some message types). Key message types:

| Type | Direction | Purpose |
| ---- | --------- | ------- |
| `UA_SUBSCRIBE` | Client → Server | Subscribe to device state updates |
| `UA_STATE` | Server → Client | Full state snapshot (on connect) |
| `UA_UPDATE` | Server → Client | Incremental state change |
| `UA_COMMAND` | Client → Server | Execute a control action |

### State model

UA Console models devices as a tree of **parameters**:

```
Device[index]
  └── MonitorOutput[index]
        ├── level (dB, -inf to 0)
        ├── mute (bool)
        ├── dim (bool)
  └── Input[channel]
        ├── gain (dB)
        ├── phantom (bool)
        ├── hi_z (bool)
        ├── phase (bool)
  └── Session
        └── sample_rate (44100, 48000, 88200, 96000, 176400, 192000)
```

Parameters are identified by path strings (e.g., `"Device/0/MonitorOutput/0/level"`).

---

## Coordinator

`UniversalAudioCoordinator` does **not** extend `DataUpdateCoordinator` — there is no polling. Instead it manages a persistent TCP connection:

```python
class UniversalAudioCoordinator:
    async def async_connect(self) -> None: ...
    async def async_disconnect(self) -> None: ...
    async def async_send_command(self, path: str, value: Any) -> None: ...
    def register_listener(self, path: str, callback) -> None: ...
```

State is stored as a dict: `self.state[path] = value`. When an `UA_UPDATE` message arrives, the coordinator updates `state[path]` and calls registered listeners for that path. Entities register listeners in `__init__` and call `self.async_write_ha_state()` on change.

### Reconnection

On TCP disconnect, the coordinator attempts reconnection with exponential backoff (1s, 2s, 4s, 8s, max 60s). During the disconnect window, entities report `available = False`.

---

## Entity → parameter mapping

### `media_player` (MonitorOutput)

| HA attribute | UA parameter path |
| ------------ | ----------------- |
| `volume_level` | `Device/N/MonitorOutput/M/level` (mapped 0.0–1.0) |
| `is_volume_muted` | `Device/N/MonitorOutput/M/mute` |
| Source selection | `Device/N/MonitorOutput/M/source` |

Volume conversion: UA level is in dB (`-inf` to `0`). Mapped to HA `volume_level` (0.0–1.0) using a logarithmic scale: `level_db = 20 * log10(volume_level)`.

### `number` (Monitor level, Input gain)

Writes directly to the UA parameter path via `async_send_command`.

### `switch` (Phantom, Hi-Z, Phase)

One switch entity per channel per parameter. Channel count is read from the state snapshot on connect.

### `sensor` (Sample rate)

Read-only from `Device/N/Session/sample_rate`. Updates via push.

---

## Config flow

```
Step 1: user_input(host, port, device_index, monitor_output_index)
  → attempt TCP connect + handshake
  → read state snapshot to verify device_index exists
  → if ok: create entry
  → if timeout/refused: show cannot_connect
```

The device_index and monitor_output_index default to 0 (first Apollo, first output). Users with multiple Apollo units or multiple monitor outputs can configure accordingly.

---

## Requirements

- UA Console must be running on the Mac when HA starts (or the integration will retry until it connects)
- "Remote Control" must be enabled in UA Console preferences
- The Mac running UA Console must be reachable from the HA host on TCP port 4710
- No authentication is required (UA Console's remote control has no auth — local network only)

---

## Notes on UA Console compatibility

Tested with UA Console 10.x on macOS. The TCP protocol has been stable across UA Console versions. If UA changes the protocol in a major release, `tcp_client.py` may need updating.

Apollo models known to work: Solo, Twin, Twin X, x4, x6, x8, x8p, x16.
Arrow, Satellite, and Volt (USB interfaces) may work if they appear in UA Console but have not been tested.

---

## Future improvements

- Plugin parameter control (Unison preamp emulations, hardware inserts)
- Input channel labels from UA Console session
- Session recording state sensor (recording/stopped)
- Multi-Mac support (UA Console on multiple machines)
- Auto-discovery via mDNS if UA Console advertises a service
