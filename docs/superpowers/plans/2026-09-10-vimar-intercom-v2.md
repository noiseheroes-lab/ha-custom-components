# Vimar Intercom v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `custom_components/vimar_intercom` from a private-deployment accessory into a Home Assistant integration a stranger can install from HACS, configure by pasting one QR payload, and forget about.

**Architecture:** All configuration moves from `const.py` into the config entry, decrypted from the Vimar QR by a new pure module `qr.py` and shaped into an immutable `RuntimeConfig` by `runtime.py`. The SIP layer keeps its module-global design (the integration is `single_config_entry`) but gains a supervisor loop with unbounded jittered backoff, per-transaction response correlation via a new pure `sip_parser.py`, and expiry-driven re-registration. All Apple push code is deleted; the doorbell surfaces as an HA event plus an `event` entity. Video reaches Home Assistant through an ffmpeg AV pipeline fed by a NAL consumer registry that replays cached SPS/PPS to every new consumer.

**Tech Stack:** Python 3.13, Home Assistant 2026.9+, `cryptography` (already in HA core), `ffmpeg` (HA `ffmpeg` dependency), `pytest` for the pure-logic suite. No new runtime dependencies.

**Spec:** `docs/vimar-intercom-v2-spec.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **No personal data.** No real SIP credentials, MAC addresses, IMEI, device UUIDs, home IP addresses, personal domains, or account identifiers — in code, comments, docs, tests, or commit messages. Examples use `192.0.2.x` (RFC 5737) or obvious placeholders.
- **No secret ever committed.** Before every commit run:
  `git diff --cached | grep -nEi "musicman|192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{2}(:[0-9a-f]{2}){5}|imei *= *\"[0-9]{15}\"|noiseheroes\.Home|AuthKey|BEGIN [A-Z ]*PRIVATE KEY"`
  Any hit that is not an obvious placeholder blocks the commit.
- **English only** in code, comments, docstrings, log messages, `strings.json`, `translations/en.json`, and all documentation. `translations/it.json` is the one place Italian belongs, and it must be a real translation.
- **HACS-valid:** `manifest.json` must keep `version`, `documentation`, `issue_tracker`, `codeowners`, `iot_class: local_push`, `config_flow: true`.
- **Home Assistant 2026.9 or newer. Python 3.13+.** No dependency that is not already in HA core.
- **Every `HomeAssistantView` sets `requires_auth = True`. No exceptions.** If something cannot present a bearer token, use a signed path — never disable auth.
- **Never log** credentials, QR payloads, decrypted QR plaintext, or full SIP messages at INFO. No module-level `setLevel`.
- Work on `main`, small verifiable commits, each ending with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Do not push.** Do not create releases or tags. Do not touch the maintainer's server or the private repo.
- Repo root for all paths below: `/Users/luca/Sites/ha-custom-components`.
- Component root: `custom_components/vimar_intercom/`.

## Decisions taken while writing this plan

These resolve gaps or contradictions in the spec. They are binding for implementers.

1. **`single_config_entry: true`.** The SIP and media layers use module-global state. Rewriting them into per-entry objects is a large, risky refactor of code that works in production and cannot be tested here against a real panel. Instead the manifest declares a single config entry, which makes the module-global design correct rather than accidental.
2. **The audio WebSocket, the MJPEG view, the push-token view and the debug view are deleted.** They exist only to serve the private mobile app. `hub.video_frame` already returns `None` unconditionally, so the MJPEG view and the camera thumbnail have never produced a frame for anyone but that app.
3. **Video reaches HA through ffmpeg.** Decrypted H.264 NALs are written as Annex-B to ffmpeg's stdin while audio RTP continues to arrive on a UDP port described by an SDP file; ffmpeg muxes both to MPEG-TS. The camera exposes that as `stream_source` over a **signed path**, so `requires_auth = True` holds.
4. **Panel addresses are configurable, not hardcoded.** `55001`/`55002` are the maintainer's plant. The QR does not carry the panel list, so panels live in the options flow as a comma-separated list of SIP extensions, defaulting to `55001`. One lock and one call button are created per configured panel.
5. **No "log verbosity" option**, despite spec §6.2 listing one. Spec §10 says verbosity belongs to Home Assistant's own `logger:` configuration; a second control would contradict it. Documented in the README instead.
6. **`USER_AGENT` stays verbatim** as a constant. It is not personal data, and the Vimar cloud is known to accept exactly this string. Changing it risks breaking registration and cannot be tested here.
7. **The proxy host defaults to `CPROXY`, falling back to `ipvdes.vimar.cloud`.** That value is already public in the current `const.py` as `SIP_SNI`/`SIP_ROUTE`. The options flow lets a user override host and port.
8. **Tests are plain `pytest`** over modules that do not import `homeassistant`. `qr.py`, `runtime.py`, `sip_parser.py`, `backoff.py` and the media registry are written to keep that true. Config-flow tests are out of scope, as in spec §3.

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `custom_components/vimar_intercom/qr.py` | Decrypt and parse the Vimar QR payload. Pure, no HA imports. |
| `custom_components/vimar_intercom/runtime.py` | `RuntimeConfig` — every runtime value derived from the config entry. Pure. |
| `custom_components/vimar_intercom/sip_parser.py` | SIP message parsing, transaction keys, expiry parsing. Pure. |
| `custom_components/vimar_intercom/backoff.py` | Reconnect delay schedule. Pure. |
| `tests/conftest.py` | Puts the repo root on `sys.path`. |
| `tests/vimar_intercom/test_qr.py` | QR decryption and parsing. |
| `tests/vimar_intercom/test_runtime.py` | Config derivation. |
| `tests/vimar_intercom/test_sip_parser.py` | Parsing and transaction correlation. |
| `tests/vimar_intercom/test_backoff.py` | Reconnect schedule. |
| `tests/vimar_intercom/test_video_registry.py` | SPS/PPS replay to new consumers. |
| `requirements-test.txt` | `pytest`, `cryptography`. |
| `pytest.ini` | Test discovery configuration. |

**Modified**

| File | Change |
|---|---|
| `custom_components/vimar_intercom/const.py` | Reduced to true constants plus config-entry key names. |
| `custom_components/vimar_intercom/__init__.py` | Debug handler and private views removed; one authenticated AV view; runtime config wiring. |
| `custom_components/vimar_intercom/hub.py` | Push sender removed; ring fires an HA event; config from entry. |
| `custom_components/vimar_intercom/sip_client.py` | Supervisor loop, transaction correlation, registration lifecycle, config from `RuntimeConfig`. |
| `custom_components/vimar_intercom/media_handler.py` | Video consumer registry with SPS/PPS replay; AV ffmpeg fed via stdin. |
| `custom_components/vimar_intercom/config_flow.py` | QR-based user, confirm, reconfigure and options flows. |
| `custom_components/vimar_intercom/camera.py` | Signed-path `stream_source`; MJPEG handler removed. |
| `custom_components/vimar_intercom/lock.py`, `button.py` | One entity per configured panel; English names via translation keys. |
| `custom_components/vimar_intercom/binary_sensor.py`, `event.py` | Translation keys; accurate registration state. |
| `custom_components/vimar_intercom/manifest.json` | `2.0.0`, HA `2026.9.0`, `single_config_entry`, `stream` dependency. |
| `custom_components/vimar_intercom/strings.json`, `translations/*.json` | Rewritten; English canonical, Italian translated. |
| `custom_components/vimar_intercom/README.md`, `ARCHITECTURE.md` | Rewritten for a stranger. |
| `README.md`, `CHANGELOG.md`, `.gitignore`, `.github/workflows/validate.yml` | Refreshed. |

**Deleted**

- `custom_components/vimar_intercom/push_sender.py`

---

### Task 1: Strip the private-app surface

Removes every line that only exists to serve a closed iOS app: APNs push, the unauthenticated WebSocket and MJPEG views, the push-token endpoint, the debug endpoint, and the import-time debug logging. After this task the component still imports and still registers with SIP; it simply has no private surface left.

**Files:**
- Delete: `custom_components/vimar_intercom/push_sender.py`
- Modify: `custom_components/vimar_intercom/__init__.py`
- Modify: `custom_components/vimar_intercom/hub.py`
- Modify: `custom_components/vimar_intercom/const.py:42-60`
- Modify: `custom_components/vimar_intercom/media_handler.py` (drop `ws_send_bytes`)
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `hub.VimarIntercomHub` without `set_ws_broadcast`, `_has_ws_clients`, or `_ws_broadcast_fn`. `media_handler` without `ws_send_bytes`. `__init__.py` exporting only `async_setup_entry` / `async_unload_entry` and the class `VimarAVStreamView`.

- [ ] **Step 1: Delete the push sender**

```bash
git rm custom_components/vimar_intercom/push_sender.py
```

- [ ] **Step 2: Remove the APNs and push-identity block from `const.py`**

Delete lines 42-60 (the `# ─── Push Notifications / Identity ───` and `# ─── APNs VoIP Push ───` blocks) entirely, including `PN_APP_ID`, `PN_TYPE`, `PN_TOKEN`, `DEVICE_IMEI`, `DEVICE_UUID`, `MY_NAME`, `APNS_KEY_PATH`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_SANDBOX`.

Then re-add only the two protocol constants the SIP registration still needs, in the `# ─── SIP ───` block:

```python
# Push-notification contact parameters the Vimar cloud expects in the
# REGISTER Contact header. These are protocol constants, not credentials —
# the token itself is generated per installation and stored in the entry.
PN_APP_ID = "toga-prod"
PN_TYPE = "firebase"
MY_NAME = "Home Assistant"
```

The file will be reduced further in Task 5; leave the rest as it is for now.

- [ ] **Step 3: Rewrite `__init__.py`**

Replace the whole file with:

```python
"""Vimar Intercom integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import media_handler as media
from .const import DOMAIN
from .hub import VimarIntercomHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["camera", "lock", "button", "event", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    hub = VimarIntercomHub()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub}

    await hub.async_start()

    hass.http.register_view(VimarAVStreamView(hub))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["hub"].async_stop()
    return ok


class VimarAVStreamView(HomeAssistantView):
    """Serve the intercom audio/video stream as MPEG-TS.

    Reachable only with a Home Assistant bearer token or a signed path;
    the camera entity uses the latter.
    """

    url = "/api/vimar_intercom/av"
    name = "api:vimar_intercom:av"
    requires_auth = True

    def __init__(self, hub: VimarIntercomHub) -> None:
        """Store the hub this view streams from."""
        self._hub = hub

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream MPEG-TS for as long as the client stays connected."""
        await self._hub.stream_opened()

        waited = 0.0
        while not self._hub.in_call and waited < 15:
            await asyncio.sleep(0.5)
            waited += 0.5

        if not self._hub.in_call:
            _LOGGER.warning("AV stream: call not established after 15s")
            await self._hub.stream_closed()
            return web.Response(status=503, text="Call not established")

        await media.start_av_ffmpeg()
        if not media.av_ffmpeg_proc:
            await self._hub.stream_closed()
            return web.Response(status=503, text="ffmpeg failed to start")

        response = web.StreamResponse()
        response.content_type = "video/mp2t"
        await response.prepare(request)

        loop = asyncio.get_running_loop()
        try:
            while media.av_ffmpeg_proc and media.av_ffmpeg_proc.poll() is None:
                chunk = await loop.run_in_executor(
                    None, media.av_ffmpeg_proc.stdout.read, 4096)
                if not chunk:
                    break
                await response.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await media.stop_av_ffmpeg()
            await self._hub.stream_closed()
        return response
```

- [ ] **Step 4: Remove push and WebSocket wiring from `hub.py`**

Apply these edits:

1. Delete `from . import push_sender` (line 9).
2. In `__init__`, delete `self._ws_broadcast_fn` and `self._has_ws_clients`.
3. Delete the `set_ws_broadcast` method.
4. In `_on_sip_state_change`, delete the `if self._ws_broadcast_fn:` block and the local `import asyncio`, leaving only the loop over `self._state_callbacks`.
5. In `stream_opened`, delete the `if self._has_ws_clients and self._has_ws_clients():` block.
6. In `_handle_broadcast`, delete the whole `elif self._ws_broadcast_fn:` branch and the `sender = push_sender.get_sender()` block at the end. The method keeps: the debug log, the call-timeout/keyframe handling, the self-initiated-ring suppression, and the ring callbacks.

`_handle_broadcast` should end up as:

```python
    async def _handle_broadcast(self, msg_type, msg):
        """React to a SIP-layer event."""
        _LOGGER.debug("[%s] %s", msg_type, msg)

        if msg_type == "call_started":
            self._start_call_timeout()
            self._start_keyframe_loop()
        elif msg_type == "call_ended":
            self._cancel_call_timeout()
            self._cancel_keyframe_loop()

        if msg_type == "ring":
            # When we placed the call ourselves the panel INVITEs us back.
            # That is the PBX echoing our own call, not a doorbell press.
            if self._auto_called or sip.in_call or sip.calling:
                _LOGGER.debug(
                    "Suppressing ring: call initiated locally "
                    "(auto_called=%s, in_call=%s, calling=%s)",
                    self._auto_called, sip.in_call, sip.calling)
                asyncio.create_task(sip.do_decline_incoming())
                return

            for cb in self._ring_callbacks:
                try:
                    cb()
                except Exception:
                    _LOGGER.exception("Ring callback error")
```

- [ ] **Step 5: Remove the WebSocket audio sink from `media_handler.py`**

Delete the module-global `ws_send_bytes` and every reference to it. In `_emit_nal` and `_queue_nal` the guard `if not nal_data or not ws_send_bytes or not self._nal_queue` becomes `if not nal_data or not self._nal_queue`. In `_nal_sender`, replace the `if ws_send_bytes:` block with a no-op `continue` for now — Task 10 replaces this whole path with the consumer registry. Also delete `_audio_broadcast_loop`'s `if ws_send_bytes:` send and the loop itself if nothing else calls it; if `setup_media` starts it, delete that call too.

Leave `send_audio` in place; it becomes unused but harmless, and Task 12 removes it if still unreferenced.

- [ ] **Step 6: Extend `.gitignore`**

```bash
cat >> .gitignore <<'EOF'

# Secrets — never commit
AuthKey*.p8
push_tokens.json
*.pem.key
secrets.yaml
.pytest_cache/
EOF
```

Note: `custom_components/vimar_intercom/vimar_rootca.pem` is a public CA certificate and must stay tracked. Do not add `*.pem`.

- [ ] **Step 7: Verify the component still imports and the private surface is gone**

Run:

```bash
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
```
Expected: `COMPILE_OK`

Run:

```bash
grep -riE "apns|pushkit|callkit|push_sender|audio_ws|push_token|_debug_log" custom_components/vimar_intercom/ || echo "CLEAN"
```
Expected: `CLEAN`

- [ ] **Step 8: Commit**

```bash
git add -A custom_components/vimar_intercom .gitignore
git diff --cached | grep -nEi "musicman|192\.168\.|[0-9a-f]{2}(:[0-9a-f]{2}){5}|noiseheroes\.Home|AuthKey" || echo "SECRET SCAN CLEAN"
git commit -m "feat(vimar): remove Apple push and private-app HTTP surface

Deletes push_sender.py, the APNs constants, the unauthenticated audio
WebSocket, MJPEG and debug views, the push-token endpoint, and the
import-time debug log handler that forced every installation to DEBUG.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: QR decryption module and the test harness

Ports the reverse-engineered `QrUtil` algorithm into the component, with the pytest harness the rest of the plan relies on.

**Files:**
- Create: `custom_components/vimar_intercom/qr.py`
- Create: `tests/conftest.py`
- Create: `tests/vimar_intercom/test_qr.py`
- Create: `requirements-test.txt`
- Create: `pytest.ini`
- Modify: `.github/workflows/validate.yml`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `qr.QRDecodeError(ValueError)`
  - `qr.REQUIRED_FIELDS: tuple[str, ...]` — `("ID", "PWD", "CDOMAIN")`
  - `qr.decrypt_payload(payload: str) -> str`
  - `qr.parse_fields(plaintext: str) -> dict[str, str]`
  - `qr.decode_qr(payload: str) -> dict[str, str]` — raises `QRDecodeError` on bad base64, failed decryption, or a missing required field.

- [ ] **Step 1: Create the test harness files**

`requirements-test.txt`:

```text
pytest>=8.0
cryptography>=42.0
```

`pytest.ini`:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
```

`tests/conftest.py`:

```python
"""Make the repository root importable so `custom_components.*` resolves."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
```

- [ ] **Step 2: Write the failing tests**

`tests/vimar_intercom/test_qr.py`:

```python
"""Tests for the Vimar QR payload decoder."""

import base64

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.vimar_intercom import qr


def build_payload(plaintext: str, key: bytes = b"K" * 32, iv: bytes = b"I" * 16) -> str:
    """Build a QR payload the way the Vimar app does: key | ciphertext | iv."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(key + ciphertext + iv).decode()


PLAINTEXT = (
    "ID=60901\n"
    "PWD=examplepassword\n"
    "CDOMAIN=abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud\n"
    "CPROXY=ipvdes.vimar.cloud\n"
    "PROXY=192.0.2.10\n"
    "GID=21\n"
    "MAC=00:00:5E:00:53:00\n"
    "PLANTTYPE=2FV2\n"
    "PC=40515\n"
)


def test_decrypt_payload_returns_plaintext():
    assert qr.decrypt_payload(build_payload(PLAINTEXT)) == PLAINTEXT


def test_parse_fields_newline_separated():
    fields = qr.parse_fields(PLAINTEXT)
    assert fields["ID"] == "60901"
    assert fields["CDOMAIN"] == "abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"
    assert fields["GID"] == "21"


def test_parse_fields_ampersand_separated():
    fields = qr.parse_fields("ID=60901&PWD=secret&CDOMAIN=example.invalid")
    assert fields == {
        "ID": "60901",
        "PWD": "secret",
        "CDOMAIN": "example.invalid",
    }


def test_parse_fields_percent_decodes_values():
    assert qr.parse_fields("ID=a%20b")["ID"] == "a b"


def test_decode_qr_round_trip():
    fields = qr.decode_qr(build_payload(PLAINTEXT))
    assert fields["ID"] == "60901"
    assert fields["MAC"] == "00:00:5E:00:53:00"


def test_decode_qr_strips_surrounding_whitespace():
    fields = qr.decode_qr("  " + build_payload(PLAINTEXT) + "\n")
    assert fields["ID"] == "60901"


def test_decode_qr_rejects_invalid_base64():
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr("this is not base64 $$$")


def test_decode_qr_rejects_short_payload():
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(base64.b64encode(b"tooshort").decode())


def test_decode_qr_rejects_wrong_key():
    payload = build_payload(PLAINTEXT, key=b"K" * 32)
    raw = bytearray(base64.b64decode(payload))
    raw[0] ^= 0xFF  # corrupt the embedded key
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(base64.b64encode(bytes(raw)).decode())


@pytest.mark.parametrize("missing", ["ID", "PWD", "CDOMAIN"])
def test_decode_qr_requires_mandatory_fields(missing):
    lines = [ln for ln in PLAINTEXT.strip().split("\n")
             if not ln.startswith(missing + "=")]
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(build_payload("\n".join(lines)))


def test_error_message_never_contains_plaintext():
    payload = build_payload(PLAINTEXT.replace("ID=60901\n", ""))
    with pytest.raises(qr.QRDecodeError) as excinfo:
        qr.decode_qr(payload)
    assert "examplepassword" not in str(excinfo.value)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m pytest tests/vimar_intercom/test_qr.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.vimar_intercom.qr'`

- [ ] **Step 4: Write `qr.py`**

```python
"""Decode the QR payload printed by the Vimar View app.

The QR encodes base64 of `key[32] || ciphertext || iv[16]`. The ciphertext
is AES-256-CBC with PKCS#7 padding, using the embedded key and IV. The
plaintext is `KEY=VALUE` pairs separated by newlines or `&`.

Reverse engineered from `com.vimar.vmsipsdk.utility.QrUtil`.

Nothing in this module logs. The decrypted payload contains the SIP
password and must never reach the log or an exception message.
"""

from __future__ import annotations

import base64
import binascii
from urllib.parse import unquote

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_LENGTH = 32
IV_LENGTH = 16
BLOCK_SIZE_BITS = 128
MIN_PAYLOAD_LENGTH = KEY_LENGTH + IV_LENGTH + 16

REQUIRED_FIELDS: tuple[str, ...] = ("ID", "PWD", "CDOMAIN")


class QRDecodeError(ValueError):
    """The QR payload could not be decoded into usable credentials.

    Messages are deliberately generic: they must be safe to show in the
    config flow and to write to the log.
    """


def decrypt_payload(payload: str) -> str:
    """Return the plaintext of a base64 Vimar QR payload.

    Raises QRDecodeError if the payload is not valid base64, is too
    short, or does not decrypt to valid UTF-8.
    """
    try:
        raw = base64.b64decode(payload.strip(), validate=True)
    except (binascii.Error, ValueError) as err:
        raise QRDecodeError("payload is not valid base64") from err

    if len(raw) < MIN_PAYLOAD_LENGTH:
        raise QRDecodeError("payload is too short to be a Vimar QR code")

    key = raw[:KEY_LENGTH]
    iv = raw[-IV_LENGTH:]
    ciphertext = raw[KEY_LENGTH:-IV_LENGTH]

    if not ciphertext or len(ciphertext) % 16:
        raise QRDecodeError("payload has an unexpected length")

    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(BLOCK_SIZE_BITS).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except (ValueError, InvalidKey) as err:
        raise QRDecodeError("payload could not be decrypted") from err

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as err:
        raise QRDecodeError("payload could not be decrypted") from err


def parse_fields(plaintext: str) -> dict[str, str]:
    """Parse `KEY=VALUE` pairs separated by newlines or `&`."""
    separator = "\n" if "\n" in plaintext else "&"
    fields: dict[str, str] = {}
    for pair in plaintext.strip().split(separator):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        fields[key.strip()] = unquote(value.strip())
    return fields


def decode_qr(payload: str) -> dict[str, str]:
    """Decrypt and parse a QR payload, checking the mandatory fields."""
    fields = parse_fields(decrypt_payload(payload))

    missing = [name for name in REQUIRED_FIELDS if not fields.get(name)]
    if missing:
        raise QRDecodeError(
            "QR code is missing required fields: " + ", ".join(missing))

    return fields
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/vimar_intercom/test_qr.py -q`
Expected: PASS, 13 passed

- [ ] **Step 6: Add the test job to CI**

In `.github/workflows/validate.yml`, append a third job:

```yaml
  tests:
    name: pytest
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -r requirements-test.txt
      - run: python -m pytest
```

- [ ] **Step 7: Commit**

```bash
git add custom_components/vimar_intercom/qr.py tests requirements-test.txt pytest.ini .github/workflows/validate.yml
git commit -m "feat(vimar): decode the Vimar QR payload

Ports the AES-256-CBC QR decryption from the reverse-engineered
QrUtil into qr.py, with a pytest suite and a CI job to run it.
The module never logs and its errors never quote the plaintext.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `RuntimeConfig` — every runtime value derived from the entry

Turns the config entry into one immutable object the rest of the component reads. This is the module that lets `const.py` shrink.

**Files:**
- Create: `custom_components/vimar_intercom/runtime.py`
- Create: `tests/vimar_intercom/test_runtime.py`
- Modify: `custom_components/vimar_intercom/const.py`

**Interfaces:**
- Consumes: `qr.decode_qr` output field names (`ID`, `PWD`, `CDOMAIN`, `CPROXY`, `PROXY`, `GID`, `MAC`, `PLANTTYPE`, `PC`).
- Produces:
  - `runtime.PanelConfig` — frozen dataclass with `address: str`, `name: str`.
  - `runtime.RuntimeConfig` — frozen dataclass, fields and properties exactly as written below.
  - `runtime.build_runtime_config(data: Mapping[str, Any], options: Mapping[str, Any]) -> RuntimeConfig`
  - `runtime.parse_panels(raw: str) -> tuple[PanelConfig, ...]`
  - `runtime.entry_data_from_qr(fields: Mapping[str, str]) -> dict[str, Any]` — builds the entry `data` dict, generating the device identity with `secrets`.

- [ ] **Step 1: Add the config-entry key names to `const.py`**

Append to `const.py`:

```python
# ─── Config entry keys ───────────────────────────────────────────
CONF_SIP_USER = "sip_user"
CONF_SIP_PASSWORD = "sip_password"
CONF_SIP_DOMAIN = "sip_domain"
CONF_CLOUD_PROXY = "cloud_proxy"
CONF_LOCAL_PROXY = "local_proxy"
CONF_GROUP_ID = "group_id"
CONF_MAC = "mac"
CONF_PLANT_TYPE = "plant_type"
CONF_PRODUCT_CODE = "product_code"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_UUID = "device_uuid"
CONF_PUSH_TOKEN = "push_token"

# ─── Options keys ────────────────────────────────────────────────
CONF_PANELS = "panels"
CONF_PREFER_LOCAL = "prefer_local"
CONF_RTP_PORT_BASE = "rtp_port_base"
CONF_SIP_PORT = "sip_port"
CONF_DOOR_COMMAND = "door_command"

# ─── Defaults ────────────────────────────────────────────────────
DEFAULT_CLOUD_PROXY = "ipvdes.vimar.cloud"
DEFAULT_SIP_PORT = 7042
DEFAULT_LOCAL_SIP_PORT = 5060
DEFAULT_GROUP_ID = "21"
DEFAULT_PANELS = "55001"
DEFAULT_DOOR_COMMAND = "OPEN_2F"
DEFAULT_RTP_PORT_BASE = 7200
DEFAULT_REGISTER_EXPIRY = 3600
```

- [ ] **Step 2: Write the failing tests**

`tests/vimar_intercom/test_runtime.py`:

```python
"""Tests for RuntimeConfig derivation."""

import hashlib

import pytest

from custom_components.vimar_intercom import runtime

QR_FIELDS = {
    "ID": "60901",
    "PWD": "examplepassword",
    "CDOMAIN": "abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud",
    "CPROXY": "ipvdes.vimar.cloud",
    "PROXY": "192.0.2.10",
    "GID": "21",
    "MAC": "00:00:5E:00:53:00",
    "PLANTTYPE": "2FV2",
    "PC": "40515",
}


def test_entry_data_from_qr_maps_every_known_field():
    data = runtime.entry_data_from_qr(QR_FIELDS)
    assert data["sip_user"] == "60901"
    assert data["sip_password"] == "examplepassword"
    assert data["sip_domain"] == "abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"
    assert data["cloud_proxy"] == "ipvdes.vimar.cloud"
    assert data["local_proxy"] == "192.0.2.10"
    assert data["group_id"] == "21"
    assert data["mac"] == "00:00:5E:00:53:00"
    assert data["plant_type"] == "2FV2"
    assert data["product_code"] == "40515"


def test_entry_data_from_qr_generates_a_unique_device_identity():
    first = runtime.entry_data_from_qr(QR_FIELDS)
    second = runtime.entry_data_from_qr(QR_FIELDS)
    assert first["device_id"] != second["device_id"]
    assert first["device_uuid"] != second["device_uuid"]
    assert first["push_token"] != second["push_token"]
    assert len(first["device_id"]) == 15
    assert first["device_id"].isdigit()


def test_entry_data_from_qr_applies_defaults():
    data = runtime.entry_data_from_qr({
        "ID": "1", "PWD": "p", "CDOMAIN": "d.invalid"})
    assert data["cloud_proxy"] == "ipvdes.vimar.cloud"
    assert data["group_id"] == "21"
    assert data["local_proxy"] == ""


def test_build_runtime_config_derives_transport():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert cfg.proxy_host == "ipvdes.vimar.cloud"
    assert cfg.proxy_port == 7042
    assert cfg.sni == "ipvdes.vimar.cloud"
    assert cfg.route == "ipvdes.vimar.cloud"
    assert cfg.local_proxy == "192.0.2.10"
    assert cfg.local_sip_port == 5060


def test_sip_ha1_is_md5_of_user_realm_password():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    expected = hashlib.md5(
        b"60901:abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud:examplepassword"
    ).hexdigest()
    assert cfg.sip_ha1 == expected


def test_panel_uri_uses_the_sip_domain():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert cfg.panel_uri("55001") == (
        "sip:55001@abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud")


def test_default_panels_is_a_single_entry():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert [p.address for p in cfg.panels] == ["55001"]
    assert cfg.default_panel.address == "55001"


def test_parse_panels_accepts_address_and_optional_name():
    panels = runtime.parse_panels("55001:Street Gate, 55002 , 55003:Garage")
    assert [(p.address, p.name) for p in panels] == [
        ("55001", "Street Gate"),
        ("55002", "Panel 55002"),
        ("55003", "Garage"),
    ]


def test_parse_panels_rejects_non_numeric_addresses():
    with pytest.raises(ValueError):
        runtime.parse_panels("55001, not-an-extension")


def test_parse_panels_rejects_an_empty_list():
    with pytest.raises(ValueError):
        runtime.parse_panels("   ")


def test_options_override_transport_and_panels():
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS),
        {
            "sip_port": 5061,
            "panels": "55001:Gate,55002:Door",
            "prefer_local": True,
            "rtp_port_base": 8000,
            "door_command": "OPEN_1F",
        },
    )
    assert cfg.proxy_port == 5061
    assert cfg.prefer_local is True
    assert [p.name for p in cfg.panels] == ["Gate", "Door"]
    assert cfg.rtp_audio_port == 8000
    assert cfg.rtp_video_port == 10000
    assert cfg.door_command == "OPEN_1F"


def test_prefer_local_switches_the_proxy_host():
    cfg = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"prefer_local": True})
    assert cfg.proxy_host == "192.0.2.10"
    assert cfg.proxy_port == 5060
    # The SNI and Route still name the cloud, which is what the cert covers.
    assert cfg.sni == "ipvdes.vimar.cloud"


def test_prefer_local_is_ignored_without_a_local_proxy():
    data = runtime.entry_data_from_qr({
        "ID": "1", "PWD": "p", "CDOMAIN": "d.invalid"})
    cfg = runtime.build_runtime_config(data, {"prefer_local": True})
    assert cfg.proxy_host == "ipvdes.vimar.cloud"


def test_runtime_config_repr_hides_the_password():
    cfg = runtime.build_runtime_config(runtime.entry_data_from_qr(QR_FIELDS), {})
    assert "examplepassword" not in repr(cfg)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python3 -m pytest tests/vimar_intercom/test_runtime.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.vimar_intercom.runtime'`

- [ ] **Step 4: Write `runtime.py`**

```python
"""Every runtime value the integration needs, derived from the config entry.

Nothing here imports Home Assistant, so it can be unit tested directly.
`RuntimeConfig` is immutable: rebuild it when the entry changes rather
than mutating it.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_CLOUD_PROXY,
    CONF_DEVICE_ID,
    CONF_DEVICE_UUID,
    CONF_DOOR_COMMAND,
    CONF_GROUP_ID,
    CONF_LOCAL_PROXY,
    CONF_MAC,
    CONF_PANELS,
    CONF_PLANT_TYPE,
    CONF_PREFER_LOCAL,
    CONF_PRODUCT_CODE,
    CONF_PUSH_TOKEN,
    CONF_RTP_PORT_BASE,
    CONF_SIP_DOMAIN,
    CONF_SIP_PASSWORD,
    CONF_SIP_PORT,
    CONF_SIP_USER,
    DEFAULT_CLOUD_PROXY,
    DEFAULT_DOOR_COMMAND,
    DEFAULT_GROUP_ID,
    DEFAULT_LOCAL_SIP_PORT,
    DEFAULT_PANELS,
    DEFAULT_RTP_PORT_BASE,
    DEFAULT_SIP_PORT,
    USER_AGENT,
)

DEVICE_ID_DIGITS = 15
VIDEO_PORT_OFFSET = 2000
AV_VIDEO_PORT_OFFSET = 12000
AV_AUDIO_PORT_OFFSET = 12002


@dataclass(frozen=True)
class PanelConfig:
    """One entrance panel, addressed by its SIP extension."""

    address: str
    name: str


@dataclass(frozen=True)
class RuntimeConfig:
    """Immutable view of the config entry, with every derived value."""

    sip_user: str
    sip_password: str = field(repr=False)
    sip_domain: str
    proxy_host: str
    proxy_port: int
    sni: str
    route: str
    local_proxy: str
    local_sip_port: int
    prefer_local: bool
    group_id: str
    mac: str
    plant_type: str
    product_code: str
    device_id: str = field(repr=False)
    device_uuid: str = field(repr=False)
    push_token: str = field(repr=False)
    panels: tuple[PanelConfig, ...]
    door_command: str
    rtp_audio_port: int
    rtp_video_port: int
    av_video_port: int
    av_audio_port: int
    user_agent: str

    @property
    def sip_ha1(self) -> str:
        """MD5(user:realm:password), the digest-auth HA1 for this account."""
        raw = f"{self.sip_user}:{self.sip_domain}:{self.sip_password}"
        return hashlib.md5(raw.encode()).hexdigest()

    @property
    def default_panel(self) -> PanelConfig:
        """The panel used when a call or door command names no target."""
        return self.panels[0]

    def panel_uri(self, address: str) -> str:
        """SIP URI for a panel extension."""
        return f"sip:{address}@{self.sip_domain}"

    @property
    def account_uri(self) -> str:
        """SIP URI of this Home Assistant account."""
        return f"sip:{self.sip_user}@{self.sip_domain}"

    @property
    def registrar_uri(self) -> str:
        """Request URI used by REGISTER."""
        return f"sip:{self.sip_domain}"


def parse_panels(raw: str) -> tuple[PanelConfig, ...]:
    """Parse a comma-separated panel list.

    Each item is `address` or `address:Friendly Name`. Addresses must be
    numeric SIP extensions. Raises ValueError on an empty or malformed list.
    """
    panels: list[PanelConfig] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        address, _, name = item.partition(":")
        address = address.strip()
        name = name.strip()
        if not address.isdigit():
            raise ValueError(f"'{address}' is not a numeric SIP extension")
        panels.append(PanelConfig(address, name or f"Panel {address}"))

    if not panels:
        raise ValueError("at least one panel address is required")
    return tuple(panels)


def entry_data_from_qr(fields: Mapping[str, str]) -> dict[str, Any]:
    """Build the config entry `data` dict from decoded QR fields.

    The device identity is generated once, here, and then reused for the
    life of the entry. It must never be a constant in source.
    """
    return {
        CONF_SIP_USER: fields["ID"],
        CONF_SIP_PASSWORD: fields["PWD"],
        CONF_SIP_DOMAIN: fields["CDOMAIN"],
        CONF_CLOUD_PROXY: fields.get("CPROXY") or DEFAULT_CLOUD_PROXY,
        CONF_LOCAL_PROXY: fields.get("PROXY", ""),
        CONF_GROUP_ID: fields.get("GID") or DEFAULT_GROUP_ID,
        CONF_MAC: fields.get("MAC", ""),
        CONF_PLANT_TYPE: fields.get("PLANTTYPE", ""),
        CONF_PRODUCT_CODE: fields.get("PC", ""),
        CONF_DEVICE_ID: "".join(
            secrets.choice("0123456789") for _ in range(DEVICE_ID_DIGITS)),
        CONF_DEVICE_UUID: str(uuid.UUID(bytes=secrets.token_bytes(16), version=4)),
        CONF_PUSH_TOKEN: secrets.token_hex(32),
    }


def build_runtime_config(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> RuntimeConfig:
    """Combine entry data and options into an immutable RuntimeConfig."""
    cloud_proxy = data.get(CONF_CLOUD_PROXY) or DEFAULT_CLOUD_PROXY
    local_proxy = data.get(CONF_LOCAL_PROXY, "")
    prefer_local = bool(options.get(CONF_PREFER_LOCAL, False)) and bool(local_proxy)

    if prefer_local:
        proxy_host = local_proxy
        proxy_port = DEFAULT_LOCAL_SIP_PORT
    else:
        proxy_host = cloud_proxy
        proxy_port = int(options.get(CONF_SIP_PORT, DEFAULT_SIP_PORT))

    rtp_base = int(options.get(CONF_RTP_PORT_BASE, DEFAULT_RTP_PORT_BASE))

    return RuntimeConfig(
        sip_user=data[CONF_SIP_USER],
        sip_password=data[CONF_SIP_PASSWORD],
        sip_domain=data[CONF_SIP_DOMAIN],
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        sni=cloud_proxy,
        route=cloud_proxy,
        local_proxy=local_proxy,
        local_sip_port=DEFAULT_LOCAL_SIP_PORT,
        prefer_local=prefer_local,
        group_id=data.get(CONF_GROUP_ID) or DEFAULT_GROUP_ID,
        mac=data.get(CONF_MAC, ""),
        plant_type=data.get(CONF_PLANT_TYPE, ""),
        product_code=data.get(CONF_PRODUCT_CODE, ""),
        device_id=data[CONF_DEVICE_ID],
        device_uuid=data[CONF_DEVICE_UUID],
        push_token=data[CONF_PUSH_TOKEN],
        panels=parse_panels(options.get(CONF_PANELS) or DEFAULT_PANELS),
        door_command=options.get(CONF_DOOR_COMMAND) or DEFAULT_DOOR_COMMAND,
        rtp_audio_port=rtp_base,
        rtp_video_port=rtp_base + VIDEO_PORT_OFFSET,
        av_video_port=rtp_base + AV_VIDEO_PORT_OFFSET,
        av_audio_port=rtp_base + AV_AUDIO_PORT_OFFSET,
        user_agent=USER_AGENT,
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/vimar_intercom/test_runtime.py -q`
Expected: PASS, 14 passed

- [ ] **Step 6: Commit**

```bash
git add custom_components/vimar_intercom/runtime.py custom_components/vimar_intercom/const.py tests/vimar_intercom/test_runtime.py
git commit -m "feat(vimar): derive every runtime value from the config entry

Adds RuntimeConfig, which turns entry data and options into one
immutable object: transport, digest HA1, panel URIs, RTP ports and
the per-installation device identity generated with secrets.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: QR-based config flow, reconfigure and options

**Files:**
- Modify: `custom_components/vimar_intercom/config_flow.py` (full rewrite)
- Modify: `custom_components/vimar_intercom/strings.json` (full rewrite)
- Modify: `custom_components/vimar_intercom/translations/en.json` (mirror of `strings.json`)
- Modify: `custom_components/vimar_intercom/translations/it.json` (real Italian translation)
- Modify: `custom_components/vimar_intercom/manifest.json`

**Interfaces:**
- Consumes: `qr.decode_qr`, `qr.QRDecodeError`, `runtime.entry_data_from_qr`, `runtime.parse_panels`, and the `CONF_*`/`DEFAULT_*` names from `const.py`.
- Produces: a config entry whose `data` is exactly `entry_data_from_qr(...)` output and whose `options` may contain `panels`, `prefer_local`, `sip_port`, `rtp_port_base`, `door_command`. `unique_id` is the MAC when present, otherwise the SIP user.

- [ ] **Step 1: Update `manifest.json`**

```json
{
  "domain": "vimar_intercom",
  "name": "Vimar Intercom",
  "version": "2.0.0",
  "config_flow": true,
  "single_config_entry": true,
  "integration_type": "hub",
  "iot_class": "local_push",
  "requirements": [],
  "dependencies": [
    "ffmpeg",
    "http",
    "stream"
  ],
  "codeowners": [
    "@noiseheroes-lab"
  ],
  "documentation": "https://github.com/noiseheroes-lab/ha-custom-components/blob/main/custom_components/vimar_intercom/README.md",
  "issue_tracker": "https://github.com/noiseheroes-lab/ha-custom-components/issues",
  "homeassistant": "2026.9.0"
}
```

- [ ] **Step 2: Write `config_flow.py`**

```python
"""Config, reconfigure and options flows for Vimar Intercom."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_DOOR_COMMAND,
    CONF_MAC,
    CONF_PANELS,
    CONF_PREFER_LOCAL,
    CONF_RTP_PORT_BASE,
    CONF_SIP_DOMAIN,
    CONF_SIP_PORT,
    CONF_SIP_USER,
    DEFAULT_DOOR_COMMAND,
    DEFAULT_PANELS,
    DEFAULT_RTP_PORT_BASE,
    DEFAULT_SIP_PORT,
    DOMAIN,
)
from .qr import QRDecodeError, decode_qr
from .runtime import entry_data_from_qr, parse_panels

_LOGGER = logging.getLogger(__name__)

CONF_QR_PAYLOAD = "qr_payload"

QR_SCHEMA = vol.Schema({
    vol.Required(CONF_QR_PAYLOAD): selector.TextSelector(
        selector.TextSelectorConfig(multiline=True)
    ),
})


class VimarIntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the integration from the QR code shown by the Vimar app."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise the flow state."""
        self._entry_data: dict[str, Any] | None = None
        self._summary: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the QR payload and decode it."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                fields = decode_qr(user_input[CONF_QR_PAYLOAD])
            except QRDecodeError as err:
                _LOGGER.debug("QR decode rejected: %s", err)
                errors["base"] = "invalid_qr"
            else:
                data = entry_data_from_qr(fields)
                await self.async_set_unique_id(
                    data[CONF_MAC] or data[CONF_SIP_USER])
                self._abort_if_unique_id_configured()
                self._entry_data = data
                return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user", data_schema=QR_SCHEMA, errors=errors)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was found and create the entry on confirmation."""
        assert self._entry_data is not None

        if user_input is not None:
            return self.async_create_entry(
                title="Vimar Intercom", data=self._entry_data)

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=_summary_placeholders(self._entry_data),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the credentials of an existing entry with a fresh QR."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                fields = decode_qr(user_input[CONF_QR_PAYLOAD])
            except QRDecodeError as err:
                _LOGGER.debug("QR decode rejected: %s", err)
                errors["base"] = "invalid_qr"
            else:
                data = entry_data_from_qr(fields)
                # Keep the identity generated at first setup: the Vimar
                # cloud tracks the registration by it.
                for key in ("device_id", "device_uuid", "push_token"):
                    data[key] = entry.data.get(key, data[key])
                await self.async_set_unique_id(
                    data[CONF_MAC] or data[CONF_SIP_USER])
                self._abort_if_unique_id_mismatch(reason="wrong_panel")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure", data_schema=QR_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return VimarIntercomOptionsFlow()


class VimarIntercomOptionsFlow(OptionsFlow):
    """Tune the things a user may want to change after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        errors: dict[str, str] = {}
        options = self.config_entry.options

        if user_input is not None:
            try:
                parse_panels(user_input[CONF_PANELS])
            except ValueError as err:
                _LOGGER.debug("Panel list rejected: %s", err)
                errors[CONF_PANELS] = "invalid_panels"
            else:
                return self.async_create_entry(data=user_input)

        schema = vol.Schema({
            vol.Required(
                CONF_PANELS,
                default=options.get(CONF_PANELS, DEFAULT_PANELS),
            ): str,
            vol.Required(
                CONF_DOOR_COMMAND,
                default=options.get(CONF_DOOR_COMMAND, DEFAULT_DOOR_COMMAND),
            ): str,
            vol.Required(
                CONF_PREFER_LOCAL,
                default=options.get(CONF_PREFER_LOCAL, False),
            ): bool,
            vol.Required(
                CONF_SIP_PORT,
                default=options.get(CONF_SIP_PORT, DEFAULT_SIP_PORT),
            ): vol.All(int, vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_RTP_PORT_BASE,
                default=options.get(CONF_RTP_PORT_BASE, DEFAULT_RTP_PORT_BASE),
            ): vol.All(int, vol.Range(min=1024, max=50000)),
        })

        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors)


def _summary_placeholders(data: dict[str, Any]) -> dict[str, str]:
    """Describe what the QR contained, with the password masked."""
    return {
        "sip_user": data[CONF_SIP_USER],
        "sip_domain": data[CONF_SIP_DOMAIN],
        "mac": data[CONF_MAC] or "not provided",
        "password": "•" * 8,
    }
```

- [ ] **Step 3: Write `strings.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Vimar Intercom",
        "description": "Open the Vimar View app, go to Settings → System → Export configuration, and scan or copy the QR code it shows. Paste the whole payload below.\n\nThe payload is encrypted; Home Assistant decrypts it locally and stores the credentials in this config entry.",
        "data": {
          "qr_payload": "QR code payload"
        }
      },
      "confirm": {
        "title": "Confirm the panel",
        "description": "The QR code decoded successfully:\n\n- SIP user: {sip_user}\n- SIP domain: {sip_domain}\n- Panel MAC: {mac}\n- Password: {password}\n\nSelect Submit to add the intercom."
      },
      "reconfigure": {
        "title": "Update the QR code",
        "description": "Paste a fresh QR payload from the Vimar View app. The existing device identity is kept so the Vimar cloud still recognises this installation.",
        "data": {
          "qr_payload": "QR code payload"
        }
      }
    },
    "error": {
      "invalid_qr": "That does not look like a Vimar QR payload. Copy the whole string, including any trailing '=' characters."
    },
    "abort": {
      "already_configured": "This intercom is already set up.",
      "single_instance_allowed": "Only one Vimar intercom can be set up.",
      "wrong_panel": "That QR code belongs to a different panel. Remove the existing entry first.",
      "reconfigure_successful": "The credentials were updated."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Vimar Intercom options",
        "description": "Panel addresses are the SIP extensions of your entrance panels, comma separated. Add ':Name' to label one, for example '55001:Street Gate, 55002:Building Door'.",
        "data": {
          "panels": "Panel addresses",
          "door_command": "Door open command",
          "prefer_local": "Prefer the panel on the local network",
          "sip_port": "SIP proxy port",
          "rtp_port_base": "RTP base port"
        }
      }
    },
    "error": {
      "invalid_panels": "Panel addresses must be numeric SIP extensions, comma separated."
    }
  },
  "entity": {
    "camera": {
      "intercom": { "name": "Intercom" }
    },
    "lock": {
      "door": { "name": "Door" }
    },
    "button": {
      "call": { "name": "Call" },
      "answer": { "name": "Answer" },
      "hang_up": { "name": "Hang up" },
      "open_door": { "name": "Open door" },
      "reconnect": { "name": "Reconnect" }
    },
    "event": {
      "doorbell": { "name": "Doorbell" }
    },
    "binary_sensor": {
      "sip_registration": { "name": "SIP registration" },
      "in_call": { "name": "In call" }
    }
  },
  "issues": {
    "registration_down": {
      "title": "Vimar Intercom is not registered",
      "description": "The intercom has not been registered with the Vimar cloud for more than five minutes, so the doorbell will not ring in Home Assistant. Home Assistant keeps retrying. Check that the panel is online and that this host can reach {proxy_host} on port {proxy_port}."
    }
  }
}
```

- [ ] **Step 4: Copy `strings.json` to `translations/en.json`**

```bash
cp custom_components/vimar_intercom/strings.json custom_components/vimar_intercom/translations/en.json
```

- [ ] **Step 5: Write `translations/it.json`**

Same structure, Italian values. Key phrases:
- `user.title`: `"Videocitofono Vimar"`
- `user.description`: `"Apri l'app Vimar View, vai in Impostazioni → Sistema → Esporta configurazione e scansiona o copia il codice QR mostrato. Incolla qui l'intero contenuto.\n\nIl contenuto è cifrato: Home Assistant lo decifra localmente e salva le credenziali in questa voce di configurazione."`
- `user.data.qr_payload`: `"Contenuto del codice QR"`
- `confirm.title`: `"Conferma il posto esterno"`
- `confirm.description`: `"Il codice QR è stato decifrato correttamente:\n\n- Utente SIP: {sip_user}\n- Dominio SIP: {sip_domain}\n- MAC del posto esterno: {mac}\n- Password: {password}\n\nSeleziona Invia per aggiungere il videocitofono."`
- `reconfigure.title`: `"Aggiorna il codice QR"`
- `error.invalid_qr`: `"Non sembra un codice QR Vimar. Copia l'intera stringa, inclusi eventuali caratteri '=' finali."`
- `abort.already_configured`: `"Questo videocitofono è già configurato."`
- `abort.single_instance_allowed`: `"È possibile configurare un solo videocitofono Vimar."`
- `abort.wrong_panel`: `"Questo codice QR appartiene a un altro posto esterno. Rimuovi prima la voce esistente."`
- `abort.reconfigure_successful`: `"Credenziali aggiornate."`
- `options.init.title`: `"Opzioni Videocitofono Vimar"`
- `options.init.data`: `panels` → `"Indirizzi dei posti esterni"`, `door_command` → `"Comando di apertura porta"`, `prefer_local` → `"Preferisci il posto esterno sulla rete locale"`, `sip_port` → `"Porta del proxy SIP"`, `rtp_port_base` → `"Porta RTP di base"`
- `options.error.invalid_panels`: `"Gli indirizzi devono essere interni SIP numerici, separati da virgola."`
- Entity names: `intercom` → `"Videocitofono"`, `door` → `"Porta"`, `call` → `"Chiama"`, `answer` → `"Rispondi"`, `hang_up` → `"Riaggancia"`, `open_door` → `"Apri porta"`, `reconnect` → `"Riconnetti"`, `doorbell` → `"Campanello"`, `sip_registration` → `"Registrazione SIP"`, `in_call` → `"In chiamata"`
- `issues.registration_down.title`: `"Il videocitofono Vimar non è registrato"`
- `issues.registration_down.description`: `"Il videocitofono non risulta registrato al cloud Vimar da più di cinque minuti, quindi il campanello non suonerà in Home Assistant. Home Assistant continua a riprovare. Verifica che il posto esterno sia acceso e che questo host raggiunga {proxy_host} sulla porta {proxy_port}."`

- [ ] **Step 6: Verify the JSON is valid and the three files agree on structure**

```bash
python3 - <<'PY'
import json, pathlib
base = pathlib.Path("custom_components/vimar_intercom")
def keys(o, p=""):
    if isinstance(o, dict):
        out = set()
        for k, v in o.items():
            out |= {p + k} | keys(v, p + k + ".")
        return out
    return set()
s = json.loads((base / "strings.json").read_text())
en = json.loads((base / "translations/en.json").read_text())
it = json.loads((base / "translations/it.json").read_text())
assert keys(s) == keys(en), sorted(keys(s) ^ keys(en))
assert keys(s) == keys(it), sorted(keys(s) ^ keys(it))
print("JSON_OK")
PY
```
Expected: `JSON_OK`

- [ ] **Step 7: Commit**

```bash
git add custom_components/vimar_intercom/config_flow.py custom_components/vimar_intercom/strings.json custom_components/vimar_intercom/translations custom_components/vimar_intercom/manifest.json
git commit -m "feat(vimar): configure by pasting the Vimar QR code

Replaces the placeholder config flow with a QR-based user step, a
confirmation step that masks the password, a reconfigure flow that
preserves the generated device identity, and an options flow for
panel addresses, transport and RTP ports.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire `RuntimeConfig` through the hub, SIP client and media handler

Removes the last hardcoded credential. After this task `const.py` holds only true constants and the SIP layer reads everything from the entry.

**Files:**
- Modify: `custom_components/vimar_intercom/const.py`
- Modify: `custom_components/vimar_intercom/__init__.py`
- Modify: `custom_components/vimar_intercom/hub.py`
- Modify: `custom_components/vimar_intercom/sip_client.py`
- Modify: `custom_components/vimar_intercom/media_handler.py`

**Interfaces:**
- Consumes: `runtime.build_runtime_config`, `runtime.RuntimeConfig`.
- Produces:
  - `sip_client.CFG: RuntimeConfig | None` and `sip_client.configure(cfg: RuntimeConfig) -> None`
  - `media_handler.CFG: RuntimeConfig | None` and `media_handler.configure(cfg: RuntimeConfig) -> None`
  - `VimarIntercomHub(cfg: RuntimeConfig)` — the constructor now takes the config.
  - `hub.config -> RuntimeConfig` property, used by the entity platforms.

- [ ] **Step 1: Reduce `const.py` to true constants**

The file becomes exactly:

```python
"""Constants for the Vimar Intercom integration.

Only true constants live here. Everything installation-specific comes
from the config entry through `runtime.RuntimeConfig`.
"""

import os

DOMAIN = "vimar_intercom"

# ─── Device info ─────────────────────────────────────────────────
MANUFACTURER = "Vimar"
MODEL = "Elvox Tab 5S Plus"

# ─── SIP protocol constants ──────────────────────────────────────
# The Vimar cloud is known to accept exactly this user agent. Changing
# it may break registration; it identifies the protocol dialect, not a
# specific installation.
USER_AGENT = ("TOGA_Googlesdk_gphone64_arm64_Android34"
              "/1.0|AppVer:2.4.0|ProtVer:1.0|")

# Push-notification contact parameters the Vimar cloud expects in the
# REGISTER Contact header. The token itself is generated per
# installation and stored in the config entry.
PN_APP_ID = "toga-prod"
PN_TYPE = "firebase"
MY_NAME = "Home Assistant"

SIP_LOCAL_PORT = 5070

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CA_PATH = os.path.join(SCRIPT_DIR, "vimar_rootca.pem")

# ─── Events ──────────────────────────────────────────────────────
EVENT_RING = "vimar_intercom_ring"

# ─── Repair issues ───────────────────────────────────────────────
ISSUE_REGISTRATION_DOWN = "registration_down"
REGISTRATION_DOWN_GRACE = 300  # seconds before raising the repair issue

# ─── Config entry keys ───────────────────────────────────────────
# ... (the CONF_* and DEFAULT_* block added in Task 3, unchanged)
```

Keep the `CONF_*` / `DEFAULT_*` / `DEFAULT_REGISTER_EXPIRY` block from Task 3 verbatim at the end. Delete `SIP_USER`, `SIP_DOMAIN`, `SIP_PASSWORD`, `SIP_HA1`, `SIP_PROXY`, `SIP_PORT`, `SIP_SNI`, `SIP_ROUTE`, `INTERCOM`, `DOOR_ESTERNO`, `DOOR_INTERNO`, `DOOR_COMMAND`, `RTP_AUDIO_PORT`, `RTP_VIDEO_PORT`, `FFMPEG_VIDEO_PORT`, `FFMPEG_AV_VIDEO_PORT`, `FFMPEG_AV_AUDIO_PORT`, `LOCAL_PROXY`, `LOCAL_SIP_PORT`.

- [ ] **Step 2: Add the configure entry points to `sip_client.py`**

At the top of the module, replace `from . import const as C` with:

```python
from . import const as C
from .runtime import RuntimeConfig

# Set once by the hub at start-up. The integration declares
# single_config_entry, so one module-global config is correct.
CFG: RuntimeConfig | None = None


def configure(cfg: RuntimeConfig) -> None:
    """Install the runtime configuration for this SIP client."""
    global CFG
    CFG = cfg
```

- [ ] **Step 3: Replace every `C.<installation value>` reference in `sip_client.py`**

Mechanical substitution across the module:

| Old | New |
|---|---|
| `C.SIP_USER` | `CFG.sip_user` |
| `C.SIP_PASSWORD` | `CFG.sip_password` |
| `C.SIP_DOMAIN` | `CFG.sip_domain` |
| `C.SIP_HA1` | `CFG.sip_ha1` |
| `C.SIP_PROXY` | `CFG.proxy_host` |
| `C.SIP_PORT` | `CFG.proxy_port` |
| `C.SIP_SNI` | `CFG.sni` |
| `C.SIP_ROUTE` | `CFG.route` |
| `C.LOCAL_PROXY` | `CFG.local_proxy` |
| `C.LOCAL_SIP_PORT` | `CFG.local_sip_port` |
| `C.INTERCOM` | `CFG.panel_uri(CFG.default_panel.address)` |
| `C.DOOR_ESTERNO` / `C.DOOR_INTERNO` | removed — callers pass a panel address |
| `C.DOOR_COMMAND` | `CFG.door_command` |
| `C.DEVICE_UUID` | `CFG.device_uuid` |
| `C.DEVICE_IMEI` | `CFG.device_id` |
| `C.PN_TOKEN` | `CFG.push_token` |
| `C.USER_AGENT` | `CFG.user_agent` |
| `C.RTP_AUDIO_PORT` | `CFG.rtp_audio_port` |
| `C.RTP_VIDEO_PORT` | `CFG.rtp_video_port` |
| `5070` (hardcoded contact port) | `C.SIP_LOCAL_PORT` |

`C.PN_APP_ID`, `C.PN_TYPE`, `C.MY_NAME`, `C.CA_PATH`, `C.SIP_LOCAL_PORT` stay as `C.` references.

`get_local_ip()` becomes:

```python
def get_local_ip():
    """Detect the local IP by opening a UDP socket toward the proxy."""
    for host, port in ((CFG.proxy_host, CFG.proxy_port),
                       (CFG.local_proxy, CFG.local_sip_port)):
        if not host:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, port))
            return sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
    _LOGGER.warning("Could not determine the local IP address")
    return "0.0.0.0"
```

- [ ] **Step 4: Do the same for `media_handler.py`**

Replace the `from .const import (...)` port imports with the same `CFG` / `configure` pattern, and substitute `RTP_AUDIO_PORT` → `CFG.rtp_audio_port`, `RTP_VIDEO_PORT` → `CFG.rtp_video_port`, `FFMPEG_AV_VIDEO_PORT` → `CFG.av_video_port`, `FFMPEG_AV_AUDIO_PORT` → `CFG.av_audio_port`.

Also move the AV SDP file out of `/tmp` and into the Home Assistant config directory is **not** required — but the fixed path `/tmp/intercom_av.sdp` collides between installations on one host. Change `_create_av_sdp` to use `tempfile`:

```python
def _create_av_sdp() -> str:
    """Write the SDP that describes the audio RTP stream for ffmpeg."""
    global _av_sdp_path
    fd, _av_sdp_path = tempfile.mkstemp(prefix="vimar_av_", suffix=".sdp")
    with os.fdopen(fd, "w") as handle:
        handle.write(
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=AV\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {CFG.av_audio_port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
    return _av_sdp_path
```

(The video half of the SDP disappears here because Task 10 feeds video through ffmpeg's stdin. Add `import tempfile` and a module-global `_av_sdp_path: str | None = None`, unlinked in `stop_av_ffmpeg`.)

- [ ] **Step 5: Take the config in the hub**

In `hub.py`:

```python
    def __init__(self, cfg: RuntimeConfig) -> None:
        """Store the runtime configuration and initialise the state."""
        self._cfg = cfg
        ...  # existing initialisation

    @property
    def config(self) -> RuntimeConfig:
        """Runtime configuration for this hub."""
        return self._cfg
```

In `async_start`, before anything else:

```python
        sip.configure(self._cfg)
        media.configure(self._cfg)
```

Replace `sip.C.SIP_DOMAIN` in `async_call`, `async_door`, `async_probe` and `async_scan` with `self._cfg.panel_uri(target)`. `async_door` becomes:

```python
    async def async_door(
        self, target: str | None = None, command: str | None = None
    ) -> tuple[bool, str]:
        """Open a door by sending a SIP MESSAGE to the entrance panel.

        The panel forwards the command to its own relay, so no active
        call is required.
        """
        address = target or self._cfg.default_panel.address
        uri = self._cfg.panel_uri(address)
        body = command or self._cfg.door_command

        _LOGGER.debug("Door command to %s (registered=%s)", address, sip.registered)

        ok, msg = await sip.do_system_message(
            uri, body, extra_headers={"Panda": "command"})
        if ok:
            _LOGGER.info("Door %s opened", address)
            return True, msg

        _LOGGER.warning("Door command to %s failed (%s); re-registering and retrying",
                        address, msg)
        try:
            if not await sip.do_register():
                return False, "Re-registration failed"
            ok, msg = await sip.do_system_message(
                uri, body, extra_headers={"Panda": "command"})
            if ok:
                _LOGGER.info("Door %s opened on retry", address)
            else:
                _LOGGER.error("Door %s failed on retry: %s", address, msg)
            return ok, msg
        except Exception as err:  # noqa: BLE001 - surfaced to the caller
            _LOGGER.error("Door retry error: %s", err)
            return False, str(err)
```

- [ ] **Step 6: Build the config in `async_setup_entry`**

In `__init__.py`:

```python
from .runtime import build_runtime_config


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Vimar Intercom from a config entry."""
    cfg = build_runtime_config(entry.data, entry.options)
    hub = VimarIntercomHub(cfg)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"hub": hub}

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hub.async_start()
    hass.http.register_view(VimarAVStreamView(hub))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
```

- [ ] **Step 7: Verify nothing references the removed constants**

```bash
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
grep -rnE "C\.(SIP_USER|SIP_PASSWORD|SIP_DOMAIN|SIP_HA1|SIP_PROXY|SIP_PORT|SIP_SNI|SIP_ROUTE|INTERCOM|DOOR_|DEVICE_|PN_TOKEN|RTP_|LOCAL_PROXY|LOCAL_SIP_PORT)" custom_components/vimar_intercom/ || echo "NO STALE CONSTANTS"
```
Expected: `COMPILE_OK` then `NO STALE CONSTANTS`

- [ ] **Step 8: Commit**

```bash
git add custom_components/vimar_intercom
git commit -m "refactor(vimar): read all configuration from the config entry

const.py now holds only true constants. The SIP client, media handler
and hub read credentials, transport, panel addresses and RTP ports
from RuntimeConfig, built from the entry at setup and rebuilt on
options changes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: SIP parsing and per-transaction response correlation

Fixes the `Stale response` defect: REGISTER replies arriving during an active dialog were discarded because responses were matched against the last known Call-ID.

**Files:**
- Create: `custom_components/vimar_intercom/sip_parser.py`
- Create: `tests/vimar_intercom/test_sip_parser.py`
- Modify: `custom_components/vimar_intercom/sip_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `sip_parser.ParsedMessage` — frozen dataclass: `code: int | None`, `method: str | None`, `headers: dict[str, str]`, `via_list: list[str]`, `body: str`, `start_line: str`.
  - `sip_parser.parse_message(raw: str) -> ParsedMessage`
  - `sip_parser.header_params(value: str) -> dict[str, str]`
  - `sip_parser.via_branch(headers: Mapping[str, str]) -> str`
  - `sip_parser.cseq_parts(headers: Mapping[str, str]) -> tuple[int | None, str]`
  - `sip_parser.transaction_key(branch: str, seq: int | None, method: str) -> str`
  - `sip_parser.response_keys(msg: ParsedMessage) -> list[str]` — every key a response could match, most specific first.
  - `sip_parser.tag_of(header_value: str) -> str`
  - `sip_parser.granted_expiry(msg: ParsedMessage, contact_user: str, requested: int) -> int`

- [ ] **Step 1: Write the failing tests**

`tests/vimar_intercom/test_sip_parser.py`:

```python
"""Tests for SIP message parsing and transaction correlation."""

from custom_components.vimar_intercom import sip_parser as sp

REGISTER_200 = (
    "SIP/2.0 200 OK\r\n"
    "Via: SIP/2.0/TLS 192.0.2.5:5070;branch=z9hG4bKabc123;rport=5070\r\n"
    "From: <sip:60901@example.invalid>;tag=fromtag\r\n"
    "To: <sip:60901@example.invalid>;tag=totag\r\n"
    "Call-ID: reg-deadbeef\r\n"
    "CSeq: 7 REGISTER\r\n"
    "Contact: <sip:60901@192.0.2.5:5070;transport=tls>;expires=1800\r\n"
    "Content-Length: 0\r\n\r\n"
)

INVITE_REQUEST = (
    "INVITE sip:60901@example.invalid SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 192.0.2.9:5060;branch=z9hG4bKzzz\r\n"
    "Via: SIP/2.0/TLS 192.0.2.8:5060;branch=z9hG4bKyyy\r\n"
    "From: <sip:55001@example.invalid>;tag=callertag\r\n"
    "To: <sip:60901@example.invalid>\r\n"
    "Call-ID: call-1234\r\n"
    "CSeq: 1 INVITE\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: 5\r\n\r\nv=0\r\n"
)


def test_parse_response_extracts_code_and_headers():
    msg = sp.parse_message(REGISTER_200)
    assert msg.code == 200
    assert msg.method is None
    assert msg.headers["call-id"] == "reg-deadbeef"
    assert msg.start_line == "SIP/2.0 200 OK"


def test_parse_request_extracts_method_and_body():
    msg = sp.parse_message(INVITE_REQUEST)
    assert msg.method == "INVITE"
    assert msg.code is None
    assert msg.body == "v=0\r\n"


def test_parse_keeps_every_via_in_order():
    msg = sp.parse_message(INVITE_REQUEST)
    assert len(msg.via_list) == 2
    assert msg.via_list[0].endswith("branch=z9hG4bKzzz")


def test_via_branch_uses_the_topmost_via():
    msg = sp.parse_message(INVITE_REQUEST)
    assert msg.headers["via"] == msg.via_list[0]
    assert sp.via_branch(msg.headers) == "z9hG4bKzzz"


def test_cseq_parts_splits_sequence_and_method():
    assert sp.cseq_parts({"cseq": "7 REGISTER"}) == (7, "REGISTER")


def test_cseq_parts_tolerates_garbage():
    assert sp.cseq_parts({"cseq": "nonsense"}) == (None, "")
    assert sp.cseq_parts({}) == (None, "")


def test_header_params_parses_quoted_and_bare_values():
    params = sp.header_params(
        'Digest realm="example.invalid", nonce=abc, qop="auth"')
    assert params["realm"] == "example.invalid"
    assert params["nonce"] == "abc"
    assert params["qop"] == "auth"


def test_header_params_stops_at_a_uri_delimiter():
    params = sp.header_params(
        "<sip:60901@192.0.2.5:5070;transport=tls>;expires=1800")
    assert params["transport"] == "tls"
    assert params["expires"] == "1800"


def test_transaction_key_is_stable():
    assert sp.transaction_key("z9hG4bKabc123", 7, "REGISTER") == (
        "z9hG4bKabc123|7|REGISTER")


def test_response_keys_prefer_branch_and_cseq_over_call_id():
    keys = sp.response_keys(sp.parse_message(REGISTER_200))
    assert keys[0] == "z9hG4bKabc123|7|REGISTER"
    assert keys[-1] == "cid:reg-deadbeef"


def test_response_keys_fall_back_to_call_id_without_a_branch():
    raw = REGISTER_200.replace(";branch=z9hG4bKabc123", "")
    keys = sp.response_keys(sp.parse_message(raw))
    assert keys == ["cid:reg-deadbeef"]


def test_two_transactions_on_one_call_id_get_different_keys():
    first = sp.response_keys(sp.parse_message(REGISTER_200))[0]
    second = sp.response_keys(sp.parse_message(
        REGISTER_200.replace("CSeq: 7", "CSeq: 8")
                    .replace("branch=z9hG4bKabc123", "branch=z9hG4bKdef456")))[0]
    assert first != second


def test_tag_of_reads_the_tag_parameter():
    assert sp.tag_of("<sip:a@b>;tag=totag") == "totag"
    assert sp.tag_of("<sip:a@b>") == ""


def test_granted_expiry_reads_the_contact_expires_parameter():
    msg = sp.parse_message(REGISTER_200)
    assert sp.granted_expiry(msg, "60901", 3600) == 1800


def test_granted_expiry_falls_back_to_the_expires_header():
    raw = REGISTER_200.replace(";expires=1800", "").replace(
        "Content-Length: 0", "Expires: 600\r\nContent-Length: 0")
    assert sp.granted_expiry(sp.parse_message(raw), "60901", 3600) == 600


def test_granted_expiry_falls_back_to_the_requested_value():
    raw = REGISTER_200.replace(";expires=1800", "")
    assert sp.granted_expiry(sp.parse_message(raw), "60901", 3600) == 3600


def test_granted_expiry_ignores_a_contact_for_another_user():
    raw = REGISTER_200.replace("<sip:60901@192.0.2.5", "<sip:99999@192.0.2.5")
    assert sp.granted_expiry(sp.parse_message(raw), "60901", 3600) == 3600
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/vimar_intercom/test_sip_parser.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.vimar_intercom.sip_parser'`

- [ ] **Step 3: Write `sip_parser.py`**

```python
"""Pure SIP text handling: parsing, transaction keys, expiry.

No sockets, no Home Assistant, no logging — everything here is a
function of its arguments so it can be unit tested directly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# A parameter value ends at a comma, a semicolon, or the closing angle
# bracket of a URI, unless it is quoted.
_PARAM_RE = re.compile(r'([\w.+-]+)\s*=\s*("([^"]*)"|[^,;>]*)')


@dataclass(frozen=True)
class ParsedMessage:
    """A SIP message split into its parts."""

    code: int | None
    method: str | None
    headers: dict[str, str]
    via_list: list[str] = field(default_factory=list)
    body: str = ""
    start_line: str = ""


def parse_message(raw: str) -> ParsedMessage:
    """Split a raw SIP message into start line, headers and body.

    Repeated headers keep the last value, except Via, where the full
    ordered list is preserved in `via_list`.
    """
    head, _, body = raw.partition("\r\n\r\n")
    lines = head.split("\r\n")
    start_line = lines[0] if lines else ""

    code: int | None = None
    method: str | None = None
    if start_line.startswith("SIP/2.0"):
        parts = start_line.split()
        if len(parts) > 1 and parts[1].isdigit():
            code = int(parts[1])
    elif start_line:
        method = start_line.split()[0]

    headers: dict[str, str] = {}
    via_list: list[str] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.strip().lower()
        value = value.strip()
        if key == "via":
            # The topmost Via is ours; keep it, not the last one seen.
            via_list.append(value)
            headers.setdefault(key, value)
            continue
        headers[key] = value

    return ParsedMessage(
        code=code,
        method=method,
        headers=headers,
        via_list=via_list,
        body=body,
        start_line=start_line,
    )


def header_params(value: str) -> dict[str, str]:
    """Parse `name=value` parameters out of a header value."""
    params: dict[str, str] = {}
    for match in _PARAM_RE.finditer(value):
        name = match.group(1).lower()
        params[name] = (match.group(3)
                        if match.group(3) is not None
                        else match.group(2).strip())
    return params


def via_branch(headers: Mapping[str, str]) -> str:
    """Return the branch parameter of the topmost Via, or ''."""
    return header_params(headers.get("via", "")).get("branch", "")


def cseq_parts(headers: Mapping[str, str]) -> tuple[int | None, str]:
    """Split the CSeq header into (sequence, method)."""
    parts = headers.get("cseq", "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None, ""
    return int(parts[0]), parts[1].upper()


def transaction_key(branch: str, seq: int | None, method: str) -> str:
    """Build the key that correlates a request with its responses."""
    return f"{branch}|{seq}|{method}"


def response_keys(msg: ParsedMessage) -> list[str]:
    """Every key this response could be waiting under, best first.

    A response carries the branch and CSeq of the request it answers, so
    that pair identifies the transaction even when several transactions
    share a Call-ID. The Call-ID key stays as a fallback for peers that
    do not echo the branch.
    """
    keys: list[str] = []
    branch = via_branch(msg.headers)
    seq, method = cseq_parts(msg.headers)
    if branch and seq is not None:
        keys.append(transaction_key(branch, seq, method))
    call_id = msg.headers.get("call-id", "")
    if call_id:
        keys.append(f"cid:{call_id}")
    return keys


def tag_of(header_value: str) -> str:
    """Return the `tag` parameter of a From/To header, or ''."""
    for part in header_value.split(";")[1:]:
        part = part.strip()
        if part.startswith("tag="):
            return part[4:].strip()
    return ""


def granted_expiry(msg: ParsedMessage, contact_user: str, requested: int) -> int:
    """Return the registration lifetime the registrar granted, in seconds.

    Prefers the `expires` parameter on our own Contact, then the Expires
    header, then the value we asked for.
    """
    contact = msg.headers.get("contact", "")
    if f"sip:{contact_user}@" in contact:
        expires = header_params(contact).get("expires")
        if expires and expires.isdigit():
            return int(expires)

    header = msg.headers.get("expires", "")
    if header.isdigit():
        return int(header)

    return requested
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/vimar_intercom/test_sip_parser.py -q`
Expected: PASS, 17 passed

- [ ] **Step 5: Use the parser and per-transaction keys in `sip_client.py`**

1. Delete the local `_parse`, `_call_id` and `_tag` helpers. Import instead:

```python
from .sip_parser import (
    ParsedMessage,
    granted_expiry,
    header_params,
    parse_message,
    response_keys,
    tag_of,
    transaction_key,
)
```

2. `_via_block(hdrs)` read a `_via_all` key that `parse_message` does not produce. Replace it with:

```python
def _via_block(msg: ParsedMessage) -> str:
    """Rebuild the Via stack of a request, for use in a response."""
    return "".join(f"Via: {via}\r\n" for via in msg.via_list)
```

and update its four callers (`handle_incoming_invite`, `handle_incoming_bye`, `handle_incoming_options`, `handle_incoming_cancel`, `request_processor`) to pass the `ParsedMessage`.

3. Replace every `kind, hdrs, body, first = _parse(raw)` with `msg = parse_message(raw)` and use `msg.code`, `msg.method`, `msg.headers`, `msg.body`, `msg.start_line`. Replace `_call_id(hdrs)` with `msg.headers.get("call-id", "")` and `_tag(x)` with `tag_of(x)`.

4. Rename `pending_responses` to `pending_transactions` and key it by transaction key:

```python
pending_transactions: dict[str, asyncio.Queue] = {}


def _open_transaction(branch: str, seq: int, method: str, call_id: str) -> str:
    """Register a transaction and return its key."""
    key = transaction_key(branch, seq, method)
    pending_transactions[key] = asyncio.Queue()
    pending_transactions.setdefault(f"cid:{call_id}", pending_transactions[key])
    return key


def _close_transaction(key: str, call_id: str) -> None:
    """Forget a transaction and its Call-ID fallback."""
    pending_transactions.pop(key, None)
    pending_transactions.pop(f"cid:{call_id}", None)
```

5. In the reader, route by the first matching key:

```python
            if msg.code is not None:
                queue = None
                for key in response_keys(msg):
                    queue = pending_transactions.get(key)
                    if queue is not None:
                        break
                if queue is not None:
                    await queue.put(raw)
                else:
                    _LOGGER.debug(
                        "Response %d matched no open transaction (%s)",
                        msg.code, msg.start_line)
            elif msg.method is not None:
                await incoming_requests.put(raw)
```

6. Replace `_wait_final(cid, timeout)` with:

```python
async def _wait_final(key: str, call_id: str, timeout: float = 15) -> list[str]:
    """Collect responses for one transaction until a final one arrives."""
    queue = pending_transactions.get(key)
    if queue is None:
        return []
    results: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=min(remaining, 3))
            except asyncio.TimeoutError:
                continue
            results.append(raw)
            msg = parse_message(raw)
            if msg.code is not None and msg.code >= 200:
                break
    finally:
        _close_transaction(key, call_id)
    return results
```

7. Every operation (`do_register`, `do_system_message`, `do_call`, `do_options`, `do_connect_profiles`, `do_hangup`) must now generate its branch **before** sending, open the transaction with it, send, then wait. Pattern:

```python
    branch = _gen()
    seq = _next_cseq()
    key = _open_transaction(branch, seq, "REGISTER", cid)
    await send(_msg(branch=branch, seq=seq))
    responses = await _wait_final(key, cid)
```

Each `_msg`/`_inv` closure takes `branch` as a parameter instead of calling `_gen()` internally. The authenticated retry opens a **new** transaction with a fresh branch and CSeq, exactly as RFC 3261 requires.

- [ ] **Step 6: Verify the component still compiles and no test regressed**

```bash
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
python3 -m pytest -q
grep -n "Stale response\|pending_responses\|def _parse(" custom_components/vimar_intercom/sip_client.py || echo "OLD CORRELATION GONE"
```
Expected: `COMPILE_OK`, all tests pass, `OLD CORRELATION GONE`

- [ ] **Step 7: Commit**

```bash
git add custom_components/vimar_intercom/sip_parser.py custom_components/vimar_intercom/sip_client.py tests/vimar_intercom/test_sip_parser.py
git commit -m "fix(vimar): correlate SIP responses per transaction

Responses were matched against the last known Call-ID, so a REGISTER
reply arriving during an active dialog was dropped as stale. Responses
are now matched on the branch and CSeq they echo, with Call-ID as a
fallback. Parsing moves into a pure, unit-tested sip_parser module.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Unbounded reconnection and a real registration lifecycle

Fixes the eleven-day outage: five attempts then permanent silence, and a registration flag that did not track the granted lifetime.

**Files:**
- Create: `custom_components/vimar_intercom/backoff.py`
- Create: `tests/vimar_intercom/test_backoff.py`
- Modify: `custom_components/vimar_intercom/sip_client.py`
- Modify: `custom_components/vimar_intercom/hub.py`

**Interfaces:**
- Consumes: `sip_parser.granted_expiry`.
- Produces:
  - `backoff.reconnect_delay(attempt: int, *, base: float = 2.0, ceiling: float = 60.0, jitter_ratio: float = 0.25, rand: Callable[[], float] = random.random) -> float`
  - `sip_client.connection_supervisor()` — the coroutine the hub runs instead of `reader_task`.
  - `sip_client.request_reconnect()` — drop the connection so the supervisor rebuilds it.
  - `sip_client.registration_expiry: float | None` — monotonic deadline of the current registration.
  - `sip_client.registered_since: float | None` — monotonic timestamp of the last successful REGISTER, `None` while down.

- [ ] **Step 1: Write the failing tests**

`tests/vimar_intercom/test_backoff.py`:

```python
"""Tests for the reconnect delay schedule."""

import pytest

from custom_components.vimar_intercom.backoff import reconnect_delay

NO_JITTER = {"jitter_ratio": 0.0}


def test_first_attempt_waits_the_base_delay():
    assert reconnect_delay(1, **NO_JITTER) == 2.0


def test_delay_doubles_each_attempt():
    delays = [reconnect_delay(n, **NO_JITTER) for n in range(1, 6)]
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]


def test_delay_is_capped_at_the_ceiling():
    assert reconnect_delay(6, **NO_JITTER) == 60.0
    assert reconnect_delay(100, **NO_JITTER) == 60.0


def test_delay_never_overflows_for_large_attempts():
    assert reconnect_delay(10_000, **NO_JITTER) == 60.0


def test_attempts_below_one_are_treated_as_the_first():
    assert reconnect_delay(0, **NO_JITTER) == 2.0
    assert reconnect_delay(-5, **NO_JITTER) == 2.0


def test_jitter_centres_on_the_nominal_delay():
    assert reconnect_delay(3, rand=lambda: 0.5) == 8.0


@pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_jitter_stays_within_the_configured_band(value):
    delay = reconnect_delay(3, rand=lambda: value)
    assert 6.0 <= delay <= 10.0


def test_jitter_actually_varies():
    assert reconnect_delay(3, rand=lambda: 0.0) != reconnect_delay(3, rand=lambda: 1.0)


def test_ceiling_is_configurable():
    assert reconnect_delay(20, ceiling=10.0, **NO_JITTER) == 10.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/vimar_intercom/test_backoff.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.vimar_intercom.backoff'`

- [ ] **Step 3: Write `backoff.py`**

```python
"""Reconnect delay schedule.

Exponential backoff with a ceiling and proportional jitter, so a fleet
of installations recovering from the same provider outage does not
reconnect in lockstep. There is deliberately no attempt limit: the SIP
connection is the whole integration, and giving up means the doorbell
stops working until Home Assistant restarts.
"""

from __future__ import annotations

import random
from collections.abc import Callable

MAX_DOUBLINGS = 32


def reconnect_delay(
    attempt: int,
    *,
    base: float = 2.0,
    ceiling: float = 60.0,
    jitter_ratio: float = 0.25,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before reconnect attempt number `attempt`.

    `attempt` is 1 for the first retry. The nominal delay doubles each
    attempt up to `ceiling`, then jitter of +/- `jitter_ratio` is applied.
    """
    steps = min(max(attempt, 1) - 1, MAX_DOUBLINGS)
    nominal = min(base * (2 ** steps), ceiling)
    if not jitter_ratio:
        return nominal
    factor = 1.0 - jitter_ratio + 2.0 * jitter_ratio * rand()
    return nominal * factor
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/vimar_intercom/test_backoff.py -q`
Expected: PASS, 13 passed

- [ ] **Step 5: Replace `reconnect()` and `reader_task()` with a supervisor**

In `sip_client.py`, delete `async def reconnect()` entirely and restructure the reader. Add near the state block:

```python
from .backoff import reconnect_delay

registration_expiry: float | None = None
registered_since: float | None = None
_reregister_task: asyncio.Task | None = None
_connection_lost: asyncio.Event | None = None
```

Add:

```python
def request_reconnect() -> None:
    """Ask the supervisor to tear down and rebuild the connection."""
    if _connection_lost is not None:
        _connection_lost.set()
    if writer is not None:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - closing a dead socket may raise
            pass


async def connection_supervisor() -> None:
    """Keep the SIP connection up forever, with jittered backoff.

    Never gives up: DNS failures, TCP failures and refused registrations
    are all treated the same way.
    """
    global _connection_lost
    _connection_lost = asyncio.Event()
    attempt = 0

    while True:
        try:
            await connect()
            if not await do_register():
                raise ConnectionError("registration was refused")
            attempt = 0
            await do_connect_profiles()
            await _reader_loop()
            raise ConnectionError("connection closed by the server")
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - any failure means retry
            _set_registered(False)
            _cancel_reregister()
            attempt += 1
            delay = reconnect_delay(attempt)
            _LOGGER.warning(
                "SIP connection unavailable (%s); retrying in %.0fs "
                "(attempt %d)", err, delay, attempt)
            await asyncio.sleep(delay)


async def _reader_loop() -> None:
    """Read and dispatch SIP messages until the connection ends."""
    _connection_lost.clear()
    buf = b""
    while not _connection_lost.is_set():
        try:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=30)
        except asyncio.TimeoutError:
            # RFC 5626 CRLF keepalive, so the proxy does not drop us.
            async with lock:
                writer.write(b"\r\n\r\n")
                await writer.drain()
            continue
        if not chunk:
            return
        buf += chunk
        buf = _dispatch_buffer(buf)
```

`_dispatch_buffer(buf) -> bytes` is the existing `while b"\r\n\r\n" in buf:` block lifted out of `reader_task`, returning the unconsumed remainder. `reader_task` is deleted.

- [ ] **Step 6: Track the granted registration lifetime**

In `do_register`, on the 200 OK path (both the direct and the post-401 path), replace the bare `_set_registered(True)` with:

```python
            _accept_registration(parse_message(raw_200))
```

and add:

```python
REGISTER_EXPIRY_SAFETY = 0.5  # re-register at half the granted lifetime


def _accept_registration(msg) -> None:
    """Record a successful registration and schedule the refresh."""
    global registration_expiry, registered_since
    granted = granted_expiry(msg, CFG.sip_user, C.DEFAULT_REGISTER_EXPIRY)
    now = time.monotonic()
    registration_expiry = now + granted
    registered_since = now
    _set_registered(True)
    _LOGGER.info("SIP registered for %ds", granted)
    _schedule_reregister(granted * REGISTER_EXPIRY_SAFETY)


def _schedule_reregister(delay: float) -> None:
    """Re-register before the current registration expires."""
    global _reregister_task
    _cancel_reregister()
    _reregister_task = asyncio.create_task(_reregister_after(delay))


def _cancel_reregister() -> None:
    global _reregister_task, registration_expiry, registered_since
    if _reregister_task is not None:
        _reregister_task.cancel()
        _reregister_task = None
    registration_expiry = None
    registered_since = None


async def _reregister_after(delay: float) -> None:
    """Sleep, then refresh the registration; reconnect if it fails."""
    try:
        await asyncio.sleep(delay)
        if not await do_register():
            _LOGGER.warning("Registration refresh failed; reconnecting")
            request_reconnect()
    except asyncio.CancelledError:
        pass
    except Exception as err:  # noqa: BLE001 - any failure means reconnect
        _LOGGER.warning("Registration refresh error (%s); reconnecting", err)
        request_reconnect()
```

Also change `registered` so it cannot claim a registration that has lapsed:

```python
def is_registered() -> bool:
    """True only while a registration granted by the registrar is valid."""
    if not registered or registration_expiry is None:
        return False
    return time.monotonic() < registration_expiry
```

Every consumer (`hub.registered`, the binary sensor) uses `sip.is_registered()`.

- [ ] **Step 7: Replace the hub's startup and keepalive with the supervisor**

In `hub.async_start`, replace the three tasks:

```python
        self._tasks.append(asyncio.create_task(sip.connection_supervisor()))
        self._tasks.append(asyncio.create_task(sip.request_processor()))
```

Delete `_auto_startup` and `_keepalive_loop` and their `create_task` calls: the supervisor registers on connect and `_reregister_after` refreshes at half the granted lifetime, so the fixed 120-second keepalive is redundant.

In `hub`, change the `registered` property to `return sip.is_registered()` and add:

```python
    async def async_reconnect(self) -> None:
        """Force the SIP connection to be rebuilt."""
        _LOGGER.info("Manual reconnect requested")
        sip.request_reconnect()
```

- [ ] **Step 8: Verify**

```bash
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
python3 -m pytest -q
grep -n "All reconnect attempts failed\|delays = \[2, 4, 8, 16, 32\]" custom_components/vimar_intercom/sip_client.py || echo "BOUNDED RECONNECT GONE"
```
Expected: `COMPILE_OK`, all tests pass, `BOUNDED RECONNECT GONE`

- [ ] **Step 9: Commit**

```bash
git add custom_components/vimar_intercom tests/vimar_intercom/test_backoff.py
git commit -m "fix(vimar): reconnect forever and track the granted registration

The client gave up after five attempts and then stayed silent until
Home Assistant restarted. A supervisor loop now reconnects with
jittered exponential backoff from 2s to a 60s ceiling and no attempt
limit. Registration state follows the lifetime the registrar granted,
refreshed at half of it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Recovery affordances — reconnect button and repair issue

**Files:**
- Modify: `custom_components/vimar_intercom/button.py`
- Modify: `custom_components/vimar_intercom/binary_sensor.py`
- Modify: `custom_components/vimar_intercom/hub.py`
- Modify: `custom_components/vimar_intercom/__init__.py`

**Interfaces:**
- Consumes: `hub.async_reconnect`, `sip.is_registered`, `sip.registered_since`, `const.ISSUE_REGISTRATION_DOWN`, `const.REGISTRATION_DOWN_GRACE`.
- Produces: `hub.set_issue_callbacks(raise_issue, clear_issue)` — the hub calls these when registration has been down for longer than the grace period and when it recovers.

- [ ] **Step 1: Add the registration watchdog to the hub**

```python
    def set_issue_callbacks(self, raise_issue, clear_issue) -> None:
        """Install the callbacks used to raise and clear the repair issue."""
        self._raise_issue = raise_issue
        self._clear_issue = clear_issue

    async def _registration_watchdog(self) -> None:
        """Raise a repair issue when registration stays down too long."""
        down_since: float | None = None
        raised = False
        while self._running:
            await asyncio.sleep(30)
            if sip.is_registered():
                if raised and self._clear_issue:
                    self._clear_issue()
                    raised = False
                down_since = None
                continue
            if down_since is None:
                down_since = time.monotonic()
            elif (not raised
                  and time.monotonic() - down_since >= REGISTRATION_DOWN_GRACE
                  and self._raise_issue):
                self._raise_issue()
                raised = True
```

Initialise `self._raise_issue = None` and `self._clear_issue = None` in `__init__`, add `import time`, import `REGISTRATION_DOWN_GRACE` from `.const`, and append the watchdog task in `async_start`.

- [ ] **Step 2: Wire the callbacks in `__init__.py`**

```python
from functools import partial

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_REGISTRATION_DOWN
```

In `async_setup_entry`, after creating the hub:

```python
    hub.set_issue_callbacks(
        partial(_raise_registration_issue, hass, cfg),
        partial(ir.async_delete_issue, hass, DOMAIN, ISSUE_REGISTRATION_DOWN),
    )
```

and add:

```python
def _raise_registration_issue(hass: HomeAssistant, cfg) -> None:
    """Tell the user the intercom has been unregistered for too long."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_REGISTRATION_DOWN,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_REGISTRATION_DOWN,
        translation_placeholders={
            "proxy_host": cfg.proxy_host,
            "proxy_port": str(cfg.proxy_port),
        },
    )
```

In `async_unload_entry`, before returning, delete the issue so a removed entry leaves no orphan:

```python
        ir.async_delete_issue(hass, DOMAIN, ISSUE_REGISTRATION_DOWN)
```

- [ ] **Step 3: Rewrite `button.py` for configured panels and add the reconnect button**

```python
"""Button platform for Vimar Intercom."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one call and one door button per configured panel."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    entities: list[ButtonEntity] = [
        VimarAnswerButton(hub, entry.entry_id),
        VimarHangupButton(hub, entry.entry_id),
        VimarReconnectButton(hub, entry.entry_id),
    ]
    for panel in hub.config.panels:
        entities.append(VimarCallButton(hub, entry.entry_id, panel))
        entities.append(VimarDoorButton(hub, entry.entry_id, panel))
    async_add_entities(entities)


def _device_info(entry_id: str) -> DeviceInfo:
    """Device entry shared by every Vimar Intercom entity."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Vimar Intercom",
        manufacturer=MANUFACTURER,
        model=MODEL,
    )


class VimarButtonBase(ButtonEntity):
    """Common wiring for the intercom buttons."""

    _attr_has_entity_name = True

    def __init__(self, hub, entry_id: str, unique_suffix: str) -> None:
        """Attach the button to the intercom device."""
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_device_info = _device_info(entry_id)


class VimarCallButton(VimarButtonBase):
    """Call one entrance panel."""

    _attr_translation_key = "call"
    _attr_icon = "mdi:phone-outgoing"

    def __init__(self, hub, entry_id: str, panel) -> None:
        """Remember which panel this button calls."""
        super().__init__(hub, entry_id, f"call_{panel.address}")
        self._panel = panel
        self._attr_name = f"Call {panel.name}"

    async def async_press(self) -> None:
        """Place the call."""
        ok, msg = await self._hub.async_call(target=self._panel.address)
        if not ok:
            _LOGGER.error("Call to %s failed: %s", self._panel.address, msg)


class VimarDoorButton(VimarButtonBase):
    """Open the door of one entrance panel."""

    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"

    def __init__(self, hub, entry_id: str, panel) -> None:
        """Remember which panel this button opens."""
        super().__init__(hub, entry_id, f"door_{panel.address}")
        self._panel = panel
        self._attr_name = f"Open {panel.name}"

    async def async_press(self) -> None:
        """Send the door command."""
        ok, msg = await self._hub.async_door(target=self._panel.address)
        if not ok:
            _LOGGER.error("Opening %s failed: %s", self._panel.address, msg)


class VimarAnswerButton(VimarButtonBase):
    """Answer an incoming intercom call."""

    _attr_translation_key = "answer"
    _attr_icon = "mdi:phone-incoming"

    def __init__(self, hub, entry_id: str) -> None:
        """Create the answer button."""
        super().__init__(hub, entry_id, "answer")

    async def async_press(self) -> None:
        """Answer the pending call."""
        ok, msg = await self._hub.async_answer()
        if not ok:
            _LOGGER.error("Answer failed: %s", msg)


class VimarHangupButton(VimarButtonBase):
    """End the current call."""

    _attr_translation_key = "hang_up"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, hub, entry_id: str) -> None:
        """Create the hang-up button."""
        super().__init__(hub, entry_id, "hangup")

    async def async_press(self) -> None:
        """Send BYE."""
        await self._hub.async_hangup()


class VimarReconnectButton(VimarButtonBase):
    """Rebuild the SIP connection without restarting Home Assistant."""

    _attr_translation_key = "reconnect"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, entry_id: str) -> None:
        """Create the reconnect button."""
        super().__init__(hub, entry_id, "reconnect")

    async def async_press(self) -> None:
        """Drop the connection so the supervisor rebuilds it."""
        await self._hub.async_reconnect()
```

- [ ] **Step 4: Give `binary_sensor.py` translation keys and the true registration state**

Replace `_attr_has_entity_name = False` / `_attr_name = "..."` on both sensors with `_attr_has_entity_name = True` plus `_attr_translation_key = "sip_registration"` and `"in_call"`. Add the shared `_device_info` helper as in `button.py`. `VimarSIPRegistrationSensor.is_on` already reads `self._hub.registered`, which Task 7 pointed at `sip.is_registered()`.

Add an `extra_state_attributes` to the registration sensor so a user can see why it is off:

```python
    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the proxy the client is talking to."""
        cfg = self._hub.config
        return {
            "proxy_host": cfg.proxy_host,
            "proxy_port": str(cfg.proxy_port),
        }
```

- [ ] **Step 5: Verify**

```bash
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
python3 -m pytest -q
```
Expected: `COMPILE_OK`, all tests pass

- [ ] **Step 6: Commit**

```bash
git add custom_components/vimar_intercom
git commit -m "feat(vimar): add a reconnect button and a registration repair issue

A config button rebuilds the SIP connection on demand, and a repair
issue is raised when registration has been down for more than five
minutes and cleared on recovery. Buttons are now created per
configured panel instead of hardcoded extensions.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: The doorbell ring becomes a public event

Replaces the deleted push notification with the contract the spec defines: an HA bus event plus the `event` entity.

**Files:**
- Modify: `custom_components/vimar_intercom/hub.py`
- Modify: `custom_components/vimar_intercom/event.py`
- Modify: `custom_components/vimar_intercom/lock.py`
- Modify: `custom_components/vimar_intercom/sip_client.py`

**Interfaces:**
- Consumes: `const.EVENT_RING`.
- Produces:
  - `hub.set_hass(hass, entry_id: str)` — the hub needs the bus and the entry id.
  - Bus event `vimar_intercom_ring` with `{"panel", "panel_name", "entry_id"}`.
  - `hub.register_ring_callback(cb)` now passes the caller's panel address: `cb(panel_address: str)`.

- [ ] **Step 1: Fire the event from the hub**

In `hub.py` add to `__init__`: `self._hass = None` and `self._entry_id = ""`, plus:

```python
    def set_hass(self, hass, entry_id: str) -> None:
        """Give the hub the bus it fires ring events on."""
        self._hass = hass
        self._entry_id = entry_id

    def _panel_for(self, caller_uri: str) -> tuple[str, str]:
        """Map an incoming caller URI to a configured panel."""
        address = caller_uri.split("@")[0].removeprefix("sip:")
        for panel in self._cfg.panels:
            if panel.address == address:
                return panel.address, panel.name
        return address, address or "unknown"
```

In `_handle_broadcast`, replace the ring-callback loop with:

```python
            address, name = self._panel_for(
                sip.pending_incoming.get("caller_uri", ""))

            if self._hass is not None:
                self._hass.bus.async_fire(EVENT_RING, {
                    "panel": address,
                    "panel_name": name,
                    "entry_id": self._entry_id,
                })

            for cb in self._ring_callbacks:
                try:
                    cb(address)
                except Exception:
                    _LOGGER.exception("Ring callback error")
```

Import `EVENT_RING` from `.const`.

In `__init__.py`'s `async_setup_entry`, call `hub.set_hass(hass, entry.entry_id)` before `await hub.async_start()`.

- [ ] **Step 2: Update `event.py`**

```python
class VimarDoorbellEvent(EventEntity):
    """Fires when an entrance panel calls this Home Assistant.

    The same ring is also published on the event bus as
    `vimar_intercom_ring`, which is the supported integration point for
    notifications and companion apps.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell"
    _attr_icon = "mdi:bell-ring"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]
```

and:

```python
    @callback
    def _handle_ring(self, panel: str) -> None:
        """Record the ring, tagged with the panel that called."""
        self._trigger_event("ring", {"panel": panel})
        self.async_write_ha_state()
        _LOGGER.info("Doorbell ring from panel %s", panel)
```

Remove the HomeKit sentence from the class docstring and use the shared `_device_info` helper.

- [ ] **Step 3: Rewrite `lock.py` for configured panels**

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one lock per configured entrance panel."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([
        VimarIntercomLock(hub, entry.entry_id, panel)
        for panel in hub.config.panels
    ])
```

The class takes `panel` instead of `key`/`name`/`door_target`/`door_command`, sets `_attr_has_entity_name = True`, `_attr_translation_key = "door"`, `_attr_name = panel.name`, `_attr_unique_id = f"{entry_id}_lock_{panel.address}"`, and calls `self._hub.async_door(target=self._panel.address)` with the command coming from `hub.config.door_command`. Delete the HomeKit sentence from the docstring; replace it with:

```python
    """A door release, modelled as a lock.

    Unlocking sends the SIP door command to the panel, which pulses its
    relay. The physical release re-locks itself after a few seconds, so
    the entity returns to locked after the same delay.
    """
```

- [ ] **Step 4: Make the incoming INVITE log English**

In `sip_client.handle_incoming_invite`, replace `await broadcast("ring", f"Chiamata da: {caller_uri}")` with `await broadcast("ring", f"Incoming call from {caller_uri}")`, and in `do_answer_incoming` replace `"Risposto!"` with `"Answered"` and `"Nessuna chiamata in arrivo"` with `"No incoming call"`.

- [ ] **Step 5: Verify**

```bash
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
grep -rn "vimar_intercom_ring" custom_components/vimar_intercom/
```
Expected: `COMPILE_OK`, and `EVENT_RING` defined in `const.py` and fired in `hub.py`

- [ ] **Step 6: Commit**

```bash
git add custom_components/vimar_intercom
git commit -m "feat(vimar): publish the doorbell ring as vimar_intercom_ring

The ring now fires an event on the Home Assistant bus carrying the
panel address, its friendly name and the entry id, alongside the
event entity. Locks and the doorbell entity follow the configured
panel list instead of hardcoded extensions.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Video consumer registry with SPS/PPS replay

Fixes the real defect from spec §8: a consumer that attaches mid-stream never receives the parameter sets and therefore never decodes anything.

**Files:**
- Modify: `custom_components/vimar_intercom/media_handler.py`
- Create: `tests/vimar_intercom/test_video_registry.py`

**Interfaces:**
- Consumes: `RuntimeConfig` ports from Task 5.
- Produces:
  - `media_handler.VideoStreamRegistry` with `add_consumer(consumer)`, `remove_consumer(consumer)`, `push_nal(nal: bytes)`, `reset()`, and the read-only property `parameter_sets -> tuple[bytes, bytes] | None`.
  - `media_handler.video_registry: VideoStreamRegistry` — the module-level instance the RTP protocol pushes into.
  - `media_handler.ANNEX_B_START = b"\x00\x00\x00\x01"`

- [ ] **Step 1: Write the failing tests**

`tests/vimar_intercom/test_video_registry.py`:

```python
"""Tests for the H.264 consumer registry and SPS/PPS replay."""

from custom_components.vimar_intercom.media_handler import (
    ANNEX_B_START,
    VideoStreamRegistry,
)

SPS = bytes([0x67, 0x42, 0x80, 0x1F])
PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
IDR = bytes([0x65, 0x11, 0x22, 0x33])
SLICE = bytes([0x41, 0x44, 0x55, 0x66])


def collector():
    """Return a (sink, received) pair."""
    received: list[bytes] = []
    return received.append, received


def annex_b(*nals: bytes) -> list[bytes]:
    """Expected framing for the given NAL units."""
    return [ANNEX_B_START + nal for nal in nals]


def test_consumer_receives_nals_in_annex_b_framing():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    assert received == annex_b(SPS, PPS, IDR)


def test_slices_before_the_first_idr_are_dropped():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SLICE)
    assert received == []


def test_idr_before_parameter_sets_is_held_until_they_arrive():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(IDR)
    assert received == []
    registry.push_nal(SPS)
    assert received == []
    registry.push_nal(PPS)
    assert received == annex_b(SPS, PPS, IDR)


def test_a_late_consumer_gets_the_cached_parameter_sets_first():
    registry = VideoStreamRegistry()
    first_sink, _ = collector()
    registry.add_consumer(first_sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.push_nal(SLICE)

    late_sink, late_received = collector()
    registry.add_consumer(late_sink)
    assert late_received == annex_b(SPS, PPS)

    registry.push_nal(SLICE)
    assert late_received == annex_b(SPS, PPS, SLICE)


def test_a_late_consumer_with_no_cached_parameter_sets_gets_nothing():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    assert received == []


def test_parameter_sets_are_replayed_before_every_idr():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    received.clear()
    registry.push_nal(IDR)
    assert received == annex_b(SPS, PPS, IDR)


def test_updated_parameter_sets_replace_the_cache():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)

    new_sps = bytes([0x67, 0x42, 0x80, 0x28])
    registry.push_nal(new_sps)
    late_sink, late_received = collector()
    registry.add_consumer(late_sink)
    assert late_received == annex_b(new_sps, PPS)


def test_removed_consumers_stop_receiving():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.remove_consumer(sink)
    received.clear()
    registry.push_nal(SLICE)
    assert received == []


def test_a_failing_consumer_is_dropped_without_affecting_the_others():
    registry = VideoStreamRegistry()

    def broken(_data: bytes) -> None:
        raise OSError("pipe closed")

    good_sink, good_received = collector()
    registry.add_consumer(broken)
    registry.add_consumer(good_sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    assert good_received == annex_b(SPS, PPS, IDR)
    registry.push_nal(SLICE)
    assert good_received == annex_b(SPS, PPS, IDR, SLICE)


def test_reset_clears_the_cache_and_the_consumers():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.reset()
    assert registry.parameter_sets is None
    received.clear()
    registry.push_nal(SLICE)
    assert received == []


def test_parameter_sets_property_reports_the_cached_pair():
    registry = VideoStreamRegistry()
    assert registry.parameter_sets is None
    registry.push_nal(SPS)
    assert registry.parameter_sets is None
    registry.push_nal(PPS)
    assert registry.parameter_sets == (SPS, PPS)


def test_empty_nals_are_ignored():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(b"")
    assert received == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/vimar_intercom/test_video_registry.py -q`
Expected: FAIL — `ImportError: cannot import name 'VideoStreamRegistry'`

- [ ] **Step 3: Write the registry in `media_handler.py`**

Insert above `RTPVideoProtocol`:

```python
ANNEX_B_START = b"\x00\x00\x00\x01"

NAL_TYPE_SLICE = 1
NAL_TYPE_IDR = 5
NAL_TYPE_SPS = 7
NAL_TYPE_PPS = 8


class VideoStreamRegistry:
    """Fan H.264 NAL units out to consumers, replaying parameter sets.

    A decoder cannot start without the SPS and PPS that describe the
    stream, and the panel only sends them next to an IDR. Every consumer
    that attaches mid-stream therefore receives the cached pair first,
    and the pair is repeated before every IDR so a decoder that lost
    sync can recover.

    Synchronous and lock-free on purpose: it is only ever driven from
    the event loop thread by the RTP protocol.
    """

    def __init__(self) -> None:
        """Start with no consumers and no cached parameter sets."""
        self._consumers: list = []
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self._pending_idr: bytes | None = None
        self._started = False

    @property
    def parameter_sets(self) -> tuple[bytes, bytes] | None:
        """The cached (SPS, PPS) pair, or None if not seen yet."""
        if self._sps is None or self._pps is None:
            return None
        return self._sps, self._pps

    def add_consumer(self, consumer) -> None:
        """Register a consumer and prime it with the parameter sets."""
        self._consumers.append(consumer)
        pair = self.parameter_sets
        if pair is not None:
            for nal in pair:
                self._send_one(consumer, nal)

    def remove_consumer(self, consumer) -> None:
        """Stop sending to a consumer."""
        if consumer in self._consumers:
            self._consumers.remove(consumer)

    def reset(self) -> None:
        """Forget consumers and cached state, e.g. when a call ends."""
        self._consumers.clear()
        self._sps = None
        self._pps = None
        self._pending_idr = None
        self._started = False

    def push_nal(self, nal: bytes) -> None:
        """Feed one complete NAL unit into the stream."""
        if not nal:
            return

        nal_type = nal[0] & 0x1F

        if nal_type == NAL_TYPE_SPS:
            self._sps = nal
            self._flush_pending()
            return

        if nal_type == NAL_TYPE_PPS:
            self._pps = nal
            self._flush_pending()
            return

        if nal_type == NAL_TYPE_IDR:
            if self.parameter_sets is None:
                self._pending_idr = nal
                return
            self._broadcast_parameter_sets()
            self._started = True
            self._broadcast(nal)
            return

        if not self._started:
            # A decoder cannot use a predicted slice before its keyframe.
            return

        self._broadcast(nal)

    def _flush_pending(self) -> None:
        """Emit the parameter sets, and any IDR that was waiting."""
        if self.parameter_sets is None:
            return
        if self._pending_idr is not None:
            self._broadcast_parameter_sets()
            self._started = True
            self._broadcast(self._pending_idr)
            self._pending_idr = None

    def _broadcast_parameter_sets(self) -> None:
        """Send the cached SPS and PPS to every consumer."""
        pair = self.parameter_sets
        if pair is None:
            return
        for nal in pair:
            self._broadcast(nal)

    def _broadcast(self, nal: bytes) -> None:
        """Send one NAL to every consumer, dropping the broken ones."""
        for consumer in list(self._consumers):
            self._send_one(consumer, nal)

    def _send_one(self, consumer, nal: bytes) -> None:
        """Send to one consumer; drop it if the sink has gone away."""
        try:
            consumer(ANNEX_B_START + nal)
        except Exception:  # noqa: BLE001 - a dead sink must not stop the rest
            _LOGGER.debug("Dropping a video consumer that stopped accepting data")
            self.remove_consumer(consumer)


video_registry = VideoStreamRegistry()
```

Note on `test_parameter_sets_are_replayed_before_every_idr`: after the first IDR the registry re-sends SPS and PPS before each subsequent IDR, which is what `push_nal`'s IDR branch does unconditionally.

- [ ] **Step 4: Point `RTPVideoProtocol` at the registry**

In `RTPVideoProtocol`, delete `_nal_queue`, `_nal_sender_task`, `_nal_sender`, `_queue_nal`, `_last_sps`, `_last_pps`, `_sps_pps_sent`, `_pending_idr`, `_flush_params_and_idr` and the whole reorder logic inside `_emit_nal`. `_emit_nal` becomes:

```python
    def _emit_nal(self, nal_data):
        """Hand a complete NAL unit to the video registry."""
        if not nal_data:
            return
        self._nal_count += 1
        video_registry.push_nal(nal_data)
```

Delete the `connection_made` body that created the queue and task, leaving the transport assignment and the log line (moved to DEBUG).

- [ ] **Step 5: Feed ffmpeg from the registry**

Rewrite `start_av_ffmpeg` / `stop_av_ffmpeg`:

```python
async def start_av_ffmpeg():
    """Start ffmpeg muxing H.264 from stdin and audio RTP into MPEG-TS."""
    global av_ffmpeg_proc, _av_consumer
    await stop_av_ffmpeg()

    sdp_path = _create_av_sdp()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "h264", "-i", "pipe:0",
        "-protocol_whitelist", "file,udp,rtp",
        "-i", sdp_path,
        "-map", "0:v", "-map", "1:a",
        "-c", "copy",
        "-f", "mpegts",
        "pipe:1",
    ]
    try:
        av_ffmpeg_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as err:
        _LOGGER.error("Could not start ffmpeg: %s", err)
        av_ffmpeg_proc = None
        return

    asyncio.create_task(_read_av_ffmpeg_stderr())
    _av_consumer = _make_ffmpeg_consumer(av_ffmpeg_proc)
    video_registry.add_consumer(_av_consumer)
    _LOGGER.info("AV pipeline started")


def _make_ffmpeg_consumer(proc):
    """Return a consumer that writes Annex-B NALs into ffmpeg's stdin."""
    def _write(data: bytes) -> None:
        if proc.poll() is not None or proc.stdin is None:
            raise BrokenPipeError("ffmpeg has exited")
        proc.stdin.write(data)
        proc.stdin.flush()
    return _write


async def stop_av_ffmpeg():
    """Stop the AV pipeline and detach it from the video registry."""
    global av_ffmpeg_proc, _av_consumer, _av_sdp_path

    if _av_consumer is not None:
        video_registry.remove_consumer(_av_consumer)
        _av_consumer = None

    if av_ffmpeg_proc:
        try:
            if av_ffmpeg_proc.stdin:
                av_ffmpeg_proc.stdin.close()
            av_ffmpeg_proc.terminate()
            await asyncio.get_running_loop().run_in_executor(
                None, av_ffmpeg_proc.wait, 3)
        except Exception:  # noqa: BLE001 - the process may already be gone
            try:
                av_ffmpeg_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        av_ffmpeg_proc = None
        _LOGGER.info("AV pipeline stopped")

    if _av_sdp_path:
        try:
            os.unlink(_av_sdp_path)
        except OSError:
            pass
        _av_sdp_path = None
```

Add module globals `_av_consumer = None` and `_av_sdp_path: str | None = None`. In `stop_media`, call `video_registry.reset()` after `stop_av_ffmpeg()`.

Writing to `proc.stdin` from the event loop can block if ffmpeg stalls. Bound the exposure by giving the pipe a small buffer and letting `BrokenPipeError` drop the consumer, which the registry already handles.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest -q`
Expected: PASS, all suites

- [ ] **Step 7: Commit**

```bash
git add custom_components/vimar_intercom/media_handler.py tests/vimar_intercom/test_video_registry.py
git commit -m "fix(vimar): replay SPS and PPS to every new video consumer

A client attaching mid-stream never received the parameter sets and so
never decoded a frame. NAL fan-out moves into a tested
VideoStreamRegistry that caches the parameter sets, primes every new
consumer with them, repeats them before each IDR, and holds an IDR
that arrives before them. ffmpeg now consumes that stream from stdin.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Camera over a signed path

**Files:**
- Modify: `custom_components/vimar_intercom/camera.py` (full rewrite)

**Interfaces:**
- Consumes: `VimarAVStreamView.url`, `hub.in_call`.
- Produces: a camera entity with `CameraEntityFeature.STREAM` whose `stream_source` is a signed URL for `/api/vimar_intercom/av`.

- [ ] **Step 1: Rewrite `camera.py`**

```python
"""Camera platform for Vimar Intercom."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)

AV_PATH = "/api/vimar_intercom/av"
SIGNATURE_LIFETIME = timedelta(minutes=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the intercom camera."""
    hub = hass.data[DOMAIN][entry.entry_id]["hub"]
    async_add_entities([VimarIntercomCamera(hub, entry.entry_id)])


class VimarIntercomCamera(Camera):
    """Live video from the entrance panel.

    Opening the stream places a SIP call to the panel, because the panel
    only sends video inside a call. Home Assistant fetches the stream
    over a signed URL, so the underlying view still requires
    authentication.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "intercom"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hub, entry_id: str) -> None:
        """Attach the camera to the intercom device."""
        super().__init__()
        self._hub = hub
        self._attr_unique_id = f"{entry_id}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Vimar Intercom",
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_streaming(self) -> bool:
        """True while a call with video is up."""
        return self._hub.in_call

    @property
    def is_on(self) -> bool:
        """The camera is always available; the stream starts on demand."""
        return True

    async def stream_source(self) -> str | None:
        """Signed URL of the MPEG-TS stream."""
        signed = async_sign_path(self.hass, AV_PATH, SIGNATURE_LIFETIME)
        return f"{get_url(self.hass, prefer_external=False)}{signed}"
```

`async_camera_image` and `handle_async_mjpeg_stream` are deliberately gone: with `CameraEntityFeature.STREAM` and a working `stream_source`, Home Assistant produces still images from the stream itself.

- [ ] **Step 2: Verify the import path of `async_sign_path`**

Run:

```bash
python3 - <<'PY'
import pathlib, re, sys
src = pathlib.Path("custom_components/vimar_intercom/camera.py").read_text()
assert "requires_auth = False" not in src
assert "async_sign_path" in src
print("CAMERA_OK")
PY
grep -rn "requires_auth" custom_components/vimar_intercom/
```
Expected: `CAMERA_OK`, and every `requires_auth` line reads `requires_auth = True`

If `homeassistant.components.http.auth.async_sign_path` does not exist in the target HA version, import it from `homeassistant.components.http` instead; the reviewer must confirm against the installed HA before approving.

- [ ] **Step 3: Commit**

```bash
git add custom_components/vimar_intercom/camera.py
git commit -m "feat(vimar): serve the camera over a signed, authenticated path

The camera exposes the MPEG-TS AV stream through Home Assistant's
stream component using a signed URL, so the view keeps
requires_auth = True. The dead MJPEG handler and thumbnail path,
which could never return a frame, are removed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Logging discipline and the English-only sweep

**Files:**
- Modify: every `.py` in `custom_components/vimar_intercom/`

**Interfaces:**
- Consumes: nothing.
- Produces: no behaviour change; only log levels, log text and comments.

- [ ] **Step 1: Translate every remaining Italian string and comment**

Run the scan and fix each hit:

```bash
grep -rniE "chiamata|registrat[oa]|serratur|citofon|portone|targa|rubrica|fallit|errore|apert|campanell|già|nessun|risposto|vuoto|riaggancia|rispondi|esterna|interna" custom_components/vimar_intercom/*.py
```

Known replacements, all in `sip_client.py` and `hub.py`:

| Italian | English |
|---|---|
| `"Non registrato"` | `"Not registered"` |
| `"Già in chiamata"` | `"Already in a call"` |
| `"Chiamata attiva!"` | `"Call established"` |
| `"Chiamata terminata"` | `"Call ended"` |
| `"Chiamata cancellata"` | `"Call cancelled"` |
| `"Nessuna chiamata in arrivo"` | `"No incoming call"` |
| `"Risposto!"` | `"Answered"` |
| `f"Auth vuoto ({code})"` | `f"Empty authentication challenge ({code})"` |
| `f"Errore: {code}"` | `f"Rejected with {code}"` |
| `"Re-registrazione fallita"` | `"Re-registration failed"` |
| `"Timeout"` | `"Timed out"` |
| `# Messages go to the targa (PE) address...` | `# Commands go to the entrance panel, which drives its own relay.` |
| `# ─── Door targets (from Tab5S rubrica ACTUATOR_LIST) ───` | `# ─── Door targets ───` |

- [ ] **Step 2: Fix the log levels**

Apply throughout:

- INFO stays only for lifecycle: setup, registration granted or lost, call started or ended, door opened, pipeline started or stopped, reconnect attempts.
- Everything protocol-level moves to DEBUG: `[SIP >>>]`, `[SIP <<<]`, `do_system_message` header dumps, `WS action received`, NAL counters, `Video pkt #`, `FU-A START`/`FU-A END`, `First video RTP from`, `RTP Video ready on`, `Detected local IP`.
- Delete these entirely, they are development scaffolding: the `NAL #%d type=...` per-NAL log, the `Flushing SPS→PPS→IDR` log, the `self.pkt_count <= 20` and `self._nal_count <= 20` special cases.
- Never log at INFO with a credential, a QR payload, or a full SIP message in the arguments. `_LOGGER.debug("[SIP >>>] %s", first_line)` logs only the start line and is fine; a full-message log is not.

Specific edits:

```python
# sip_client.get_local_ip
_LOGGER.debug("Detected local IP %s", ip)

# sip_client.connect
_LOGGER.info("Connecting to the SIP proxy %s:%d", CFG.proxy_host, CFG.proxy_port)
...
_LOGGER.info("SIP TLS connection established")

# sip_client.do_system_message — was INFO with the body
_LOGGER.debug("Sending %s to %s", body_text, target_uri)

# hub.stream_opened
_LOGGER.debug("Stream opened (%d viewers)", self._stream_viewers)
```

- [ ] **Step 3: Verify the sweep**

```bash
grep -rniE "chiamata|registrat[oa]|serratur|citofon|portone|targa|rubrica|fallit|errore|apert|campanell|già|nessun|risposto" custom_components/vimar_intercom/*.py || echo "NO ITALIAN IN CODE"
grep -rn "setLevel" custom_components/vimar_intercom/ || echo "NO FORCED LOG LEVEL"
python3 -m compileall -q custom_components/vimar_intercom && echo COMPILE_OK
python3 -m pytest -q
```
Expected: `NO ITALIAN IN CODE`, `NO FORCED LOG LEVEL`, `COMPILE_OK`, tests pass

- [ ] **Step 4: Commit**

```bash
git add custom_components/vimar_intercom
git commit -m "chore(vimar): English-only code and honest log levels

Translates every remaining Italian log message and comment, moves
protocol traces to DEBUG, keeps INFO for lifecycle events only, and
removes the per-NAL development logging that produced hundreds of
thousands of lines in the reference deployment.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 13: Documentation, changelog and the definition of done

**Files:**
- Modify: `custom_components/vimar_intercom/README.md` (full rewrite)
- Modify: `custom_components/vimar_intercom/ARCHITECTURE.md` (full rewrite)
- Modify: `README.md` (Vimar section)
- Modify: `CHANGELOG.md`
- Modify: `hacs.json`

**Interfaces:**
- Consumes: the entity names, event name and option names produced by every earlier task.
- Produces: documentation only.

- [ ] **Step 1: Write `custom_components/vimar_intercom/README.md`**

Sections, in this order, all in English, no mention of any private app:

1. **Title and one-paragraph summary.** What it does: doorbell events, live video, door release and call control for Vimar Elvox video door entry systems, over the same cloud SIP protocol the Vimar View app uses.
2. **Verified hardware.** "Developed and tested against a Vimar Elvox Tab 5S Plus (40515/40517) on a 2-wire Due Fili Plus system. Other panels speak the same protocol and may work, but are untested — please open an issue with your results."
3. **Requirements.** The Vimar View app, already paired with the panel; the QR payload it can export; a Home Assistant host that can reach `ipvdes.vimar.cloud` on TCP 7042; `ffmpeg` (bundled with Home Assistant OS/Container).
4. **Installation.** HACS custom repository, then a manual `custom_components/vimar_intercom` copy. Restart, then **Settings → Devices & services → Add integration → Vimar Intercom**.
5. **Setup.** Paste the QR payload. Confirm the summary. Done.
6. **Entities table:**

   | Entity | Type | Notes |
   |---|---|---|
   | `camera.vimar_intercom_intercom` | camera | Opening the stream places a call to the panel |
   | `event.vimar_intercom_doorbell` | event | Event type `ring`, attribute `panel` |
   | `lock.vimar_intercom_<panel>` | lock | Unlock pulses the door release; re-locks itself |
   | `button.vimar_intercom_call_<panel>` | button | Call that panel |
   | `button.vimar_intercom_open_<panel>` | button | Open that panel's door |
   | `button.vimar_intercom_answer` / `_hangup` | button | Answer or end a call |
   | `button.vimar_intercom_reconnect` | button | Rebuild the SIP connection |
   | `binary_sensor.vimar_intercom_sip_registration` | binary_sensor | Connectivity; on only while registered |
   | `binary_sensor.vimar_intercom_in_call` | binary_sensor | A call is up |

7. **Automation examples**, both using the bus event:

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

8. **Options.** Panel addresses (with the `address:Name` syntax and how to find them: they are printed on the panel's address label and shown in the Vimar View address book), door open command, prefer local panel, SIP proxy port, RTP base port.
9. **Troubleshooting.**
   - *Registration stays off* — check the host can reach the proxy shown in the sensor's attributes; press the Reconnect button; the integration retries forever, so a repair issue after five minutes means the panel or the network, not Home Assistant.
   - *No video* — video only flows inside a call, so the camera is black until something opens the stream; check `ffmpeg` is present; check the RTP base port is not firewalled.
   - *Door does not open* — confirm the panel address in the options; some plants use a different command than `OPEN_2F`.
   - *More detail in the log*:

```yaml
logger:
  logs:
    custom_components.vimar_intercom: debug
```

   with a warning that DEBUG includes protocol traces and should not be left on.
10. **How it works, honestly.** "This integration speaks the SIP dialect the Vimar cloud uses for the Vimar View app. That dialect is not documented; it was reverse engineered from the app and from captured traffic. It works today and it can break the day Vimar changes something. It does not use any Vimar partner API and it is not affiliated with or endorsed by Vimar."
11. **License.** MIT.

- [ ] **Step 2: Write `ARCHITECTURE.md`**

Keep the protocol notes that are already there, made generic, and add:

- **Module map** — one line per module, matching the File Structure table in this plan.
- **Connection state machine** — `disconnected → connecting → registering → registered → (call) → registered`, with every edge back to `disconnected` going through the supervisor's backoff, and a note that there is no terminal failure state by design.
- **Transaction model** — responses correlate on `branch|CSeq|method`, Call-ID as fallback; each authenticated retry is a new transaction with a fresh branch.
- **Media pipeline** — SRTP in, depacketise to NALs, `VideoStreamRegistry` fans out with parameter-set replay, ffmpeg muxes stdin H.264 plus RTP audio to MPEG-TS, camera reads it over a signed path.
- **Threat model** — the entry holds SIP credentials in the Home Assistant config entry store, so anyone with access to `.storage` has them; every HTTP view requires authentication because the door release is reachable from them; the signed camera URL expires in ten minutes; the integration never opens an inbound port on the internet, it maintains an outbound TLS connection.

- [ ] **Step 3: Update the repository `README.md`**

Replace the Vimar block with:

```markdown
### 🔔 [Vimar Intercom](custom_components/vimar_intercom/)

Integrate **Vimar Elvox** video door entry systems (verified on Tab 5S Plus, 40515/40517) into Home Assistant. Configure by pasting the QR code from the Vimar View app: doorbell events, live camera, door release, and call control.

**Entities:** camera, doorbell event, locks, call and door buttons, SIP registration sensor

**IoT class:** Local push (SIP) · **Version:** 2.0.0
```

- [ ] **Step 4: Update `CHANGELOG.md`**

Prepend:

```markdown
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
- Every new video consumer receives the cached SPS and PPS, so a client
  attaching mid-stream can decode.

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
```

- [ ] **Step 5: Update `hacs.json`**

```json
{
  "name": "NoiseHeroes HA Custom Components",
  "render_readme": true,
  "homeassistant": "2026.9.0"
}
```

- [ ] **Step 6: Run the full definition-of-done check**

```bash
cd /Users/luca/Sites/ha-custom-components

echo "--- §12.1 no private-app traces ---"
grep -riE "apns|pushkit|callkit|swift|iphone|bundle_id|noiseheroes\.Home" custom_components/vimar_intercom/ && echo "FAIL" || echo "PASS"

echo "--- §12.2 no Italian outside it.json ---"
grep -rniE "chiamata|registrat[oa]|serratur|citofon|portone|campanell|già|nessun" \
  --include="*.py" --include="*.md" --include="strings.json" \
  custom_components/vimar_intercom/ && echo "FAIL" || echo "PASS"

echo "--- §12.5 tests ---"
python3 -m pytest -q

echo "--- no unauthenticated views ---"
grep -rn "requires_auth" custom_components/vimar_intercom/
grep -rn "requires_auth = False" custom_components/vimar_intercom/ && echo "FAIL" || echo "PASS"

echo "--- version bumped ---"
grep '"version"' custom_components/vimar_intercom/manifest.json

echo "--- no secrets ---"
git log --oneline -20
git grep -nEi "musicman|192\.168\.|[0-9a-f]{2}(:[0-9a-f]{2}){5}|AuthKey" -- custom_components docs README.md CHANGELOG.md && echo "REVIEW EACH HIT" || echo "PASS"
```

Every line must print `PASS`, the tests must be green, and the version must read `2.0.0`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs(vimar): rewrite the documentation for v2.0.0

README, ARCHITECTURE, repository README and CHANGELOG rewritten for
someone who owns a Vimar panel and has never met the author, including
the migration note for users coming from 1.x.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## What this plan cannot verify

Stated plainly, because the definition of done in the spec assumes a live system that is not available here:

- **A fresh install on a clean Home Assistant** (§12.3) and **the ten-minute network outage** (§12.4) cannot be exercised from this machine. Every task's verification is limited to compilation, the unit suite, and static checks. The reviewer for each task must not claim otherwise.
- **`hassfest` and HACS validation** run in CI on push. Since this plan does not push, they are unverified locally. Run them by opening a pull request when the work is ready.
- **The camera path** (Task 11) depends on `async_sign_path` being importable at the location used and on Home Assistant's `stream` component accepting a signed internal URL. This is the least certain part of the plan and should be the first thing tested against a real installation.
- **The proxy host derivation** (`CPROXY`, defaulting to `ipvdes.vimar.cloud`) is inferred from the existing `SIP_SNI`/`SIP_ROUTE` constants. If registration fails against a real panel, the options flow's SIP proxy port and the `prefer_local` switch are the intended escape hatches, and the derivation in `runtime.build_runtime_config` is the place to correct it.
