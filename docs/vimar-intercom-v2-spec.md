# Vimar Intercom v2 — Specification

Status: approved, ready for implementation
Repo: `noiseheroes-lab/ha-custom-components` (public, MIT, already shared on the Home Assistant community forum)
Component: `custom_components/vimar_intercom`
Local clone: `/Users/luca/Sites/ha-custom-components`
Reference implementation (v1.3.0): the maintainer's own Home Assistant deployment, not part of this repository.

## 1. Why this rewrite

The component works, but it grew inside a private project. It carries three problems that matter now that the repository is public:

1. **It leaks its origin.** APNs push, an Apple bundle identifier, and endpoints designed for one specific private iOS app. None of it is usable by anyone else, and it makes the integration look like an accessory to a closed product.
2. **It is not installable by a stranger.** Every SIP credential lives in `const.py` as a placeholder the user must edit by hand. There is no config flow worth the name: `async_step_user` creates an entry with an empty dict.
3. **It gives up.** The SIP client stops reconnecting after five attempts, then stays down until Home Assistant restarts. In the reference deployment this caused an eleven-day outage after a transient DNS failure at the provider.

## 2. Goal

A Home Assistant custom integration that a Vimar Elvox owner can install from HACS, configure by pasting the QR code printed by the Vimar app, and forget about. Professional tone, English only, no personal data, no Apple-specific code.

## 3. Scope

### In scope
- QR-based config flow, options flow, reconfigure flow
- All configuration moved from `const.py` into the config entry
- Unbounded, jittered SIP reconnection and correct transaction correlation
- Doorbell ring surfaced as an HA event and an `event` entity
- Camera, locks, call buttons, SIP status binary sensor
- Documentation rewritten for a stranger
- Tests for the pure logic (QR decryption, SIP parsing, reconnect schedule)

### Out of scope
- **APNs / PushKit / CallKit.** Removed entirely from this repository (see §5).
- Any reference to a specific private mobile app, its bundle identifier, or its author's home.
- Support for Vimar panels the maintainer cannot test. Document what is verified: Tab 5S Plus (40515/40517).

## 4. Absolute constraints

- **No personal data.** No real SIP credentials, no MAC addresses, no IMEI, no device UUID, no home IP addresses, no personal domains, no account identifiers. Examples use `192.0.2.x` (RFC 5737 documentation range) or obvious placeholders. Verified today: the current repository and its full git history are clean. Keep them clean.
- **English only** in code, comments, docstrings, log messages, strings, and documentation. The current code has Italian comments and log strings; translate them. User-facing strings live in `strings.json` and `translations/`, with `it.json` kept as a real translation.
- **No secret ever committed.** `AuthKey.p8`, `push_tokens.json`, and anything resembling a token stay out. Keep them in `.gitignore`.
- **HACS-valid**: `hacs.json`, `manifest.json` with `version`, `documentation`, `issue_tracker`, `codeowners`, `iot_class: local_push`, `config_flow: true`.
- Home Assistant 2026.9 or newer. Python 3.13+. No dependency that is not already in HA core, except `pyjwt` if it survives the APNs removal (it should not).

## 5. Architecture decision: the push sender leaves

The reference deployment needs an Apple VoIP push so that a private iOS app can raise a CallKit screen when the doorbell rings. That requirement is real but personal.

**Decision.** The public integration stops at the event. When a call arrives it fires:

```python
hass.bus.async_fire("vimar_intercom_ring", {
    "panel": "<panel id>",          # e.g. "55001"
    "panel_name": "<friendly name>",
    "entry_id": entry.entry_id,
})
```

and updates the existing `event.doorbell` entity. Anything else — the official companion app, a notify service, a private push bridge — subscribes to that. `push_sender.py` is deleted from this repository, along with `APNS_*` in `const.py`, the init block in `__init__.py`, and every mention in `hub.py` and the README.

The private VoIP bridge is a separate, unpublished component. It is not this repository's concern and must not be referenced by it.

Consequence to keep in mind: `/api/vimar_intercom/push_token` disappears from the public integration.

## 6. Configuration flow

### 6.1 What the QR contains

The Vimar app shows a QR whose payload is base64. Decoded it is `key[32] || ciphertext || iv[16]`, decrypted with AES-256-CBC (PKCS#7) using the embedded key and IV. The plaintext is `KEY=VALUE` pairs separated by newlines or `&`:

| Key | Meaning | Required |
|---|---|---|
| `ID` | SIP user / PIM identifier | yes |
| `PWD` | SIP password | yes |
| `CDOMAIN` | cloud SIP domain | yes |
| `GID` | door relay group id | no, default `21` |
| `MAC` | panel MAC, used as `unique_id` | no but strongly preferred |
| `CPROXY` | cloud proxy hostname | no |
| `PROXY` | local panel address | no |
| `PLANTTYPE`, `PC` | plant type and product code | no |

A reference implementation of the decryption exists in Swift in the private app and in Python in the maintainer's research notes. Port it to `qr.py` in this component with unit tests. Never log the decrypted payload.

### 6.2 Steps

1. `async_step_user` — a form with one multiline text field, `qr_payload`, plus a short explanation of where to find the QR in the Vimar app. On submit, decrypt and parse.
   - Invalid base64, failed decryption, or missing `ID`/`PWD`/`CDOMAIN` → `errors["base"] = "invalid_qr"`.
   - Success → `async_set_unique_id(mac or id)`, `_abort_if_unique_id_configured()`, then show a confirmation step listing what was found, with the password masked.
2. `async_step_reconfigure` — same form, updates the existing entry and reloads it.
3. `OptionsFlow` — the few things a user may want to tune after setup: the entrance panel addresses, the door open command, whether to prefer the local panel over the cloud, the SIP proxy port, and the RTP port base. Nothing secret belongs here that is not already in the entry.

   Log verbosity is deliberately **not** an option: §10 puts it in Home Assistant's own `logger:` configuration, and a second control would contradict it.

   Panel addresses cannot come from the QR — it does not carry them — and must not be hardcoded, since the extensions in any one installation belong to that plant, not to the protocol.

### 6.3 Derived values

Everything currently hardcoded is computed at runtime from the entry: `SIP_HA1`, proxy host and port, SNI, intercom and door URIs, user agent. The device identity (an emulated device id and the push registration token the Vimar cloud expects) is generated once at first setup with `secrets.token_hex`, stored in the entry, and reused. It must never be a constant in source.

After this change `const.py` holds only true constants: default ports, timeouts, the user-agent template, and the domain string.

## 7. SIP robustness

The current client fails as follows, and each item is a required fix:

1. **Bounded reconnection.** Five attempts, then `All reconnect attempts failed` and permanent silence. Replace with an unbounded loop, exponential backoff from 2 s to a 60 s ceiling, plus jitter. DNS failures and TCP failures are treated identically.
2. **Response correlation.** Responses are matched against the last known Call-ID, so REGISTER replies during an active dialog are discarded as `Stale response`. Correlate per transaction: branch parameter and CSeq, falling back to Call-ID.
3. **Registration state.** `binary_sensor.intercom_sip` must reflect reality: on only after a 200 to REGISTER and within the granted expiry. Re-register at half the granted lifetime.
4. **Recovery affordances.** A `button.intercom_reconnect` entity, and an HA repair issue raised when registration has been down for more than five minutes, cleared on recovery.

## 8. Media

`media_handler` must cache the most recent SPS and PPS NAL units and send them to every new video consumer before the next frame, followed by the next IDR. Without this a client that connects mid-stream never decodes anything. This is a real defect observed in the reference deployment.

## 9. HTTP views and authentication

Every view registered by this integration sets `requires_auth = True`. No exceptions.

The reference deployment shipped `audio_ws`, `video`, and `av` with `requires_auth = False`. Anyone on the same network could open a WebSocket and send `{"action":"door"}` to unlock the street gate. This was found and fixed in the deployment on 2026-09-10 and must never reappear. If a view needs to be reachable by something that cannot present a bearer token, that is a design error, not a reason to disable authentication.

## 10. Logging

Remove the module-level `setLevel(logging.DEBUG)` and the ring-buffer handler installed at import time. They forced every installation to debug and produced 553.000 lines in six months in the reference deployment. Log at INFO for lifecycle events, DEBUG for protocol traces, and let the user opt in through Home Assistant's own `logger:` configuration. Never log credentials, QR payloads, or full SIP messages at INFO.

## 11. Documentation

`README.md` for the component, rewritten for someone who owns a Vimar panel and has never met the author:

- What it does and what hardware is verified
- Requirements: the Vimar app, a QR code, network reachability
- Installation via HACS, then via manual copy
- Setup: paste the QR, done
- Entities table
- Automation examples using `vimar_intercom_ring`
- Troubleshooting: registration down, no video, door does not open
- A short, honest "how it works" section: this speaks the Vimar cloud SIP dialect, it is reverse engineered, it can break when Vimar changes something
- No mention of any private app

`ARCHITECTURE.md`: protocol notes, state machine, threat model. Keep it, make it generic. The repository-level `README.md` and `DEVELOPER.md` need only the Vimar section refreshed.

## 12. Definition of done

- `grep -riE "apns|pushkit|callkit|swift|iphone|bundle_id|noiseheroes\.Home"` over the component returns nothing.
- No Italian outside `translations/it.json`.
- A fresh install on a clean Home Assistant, configured only by pasting a QR, produces working entities.
- Killing the network for ten minutes and restoring it leaves the integration registered without a restart.
- `python -m pytest` green for QR decryption, SIP transaction correlation, reconnect schedule, and SPS/PPS replay.
- `hassfest` and `HACS` validation pass in CI. Add the standard Home Assistant `hassfest` and `HACS` GitHub Actions if absent.
- Version bumped to `2.0.0` in `manifest.json`, `CHANGELOG.md` updated with a migration note: users upgrading from 1.x must reconfigure through the QR flow, since credentials move out of `const.py`.

## 13. Suggested order

1. Repository hygiene: CI workflows, `.gitignore`, remove Apple code, translate Italian. One commit each.
2. `qr.py` plus tests, then the config flow, options and reconfigure.
3. Move every constant into the entry; `const.py` slimmed.
4. SIP robustness, with tests.
5. SPS/PPS replay.
6. Authentication on views, logging cleanup.
7. Documentation and CHANGELOG, version bump.

Each step keeps the component importable and the test suite green.
