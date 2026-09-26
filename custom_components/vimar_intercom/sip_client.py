"""Vimar Intercom — SIP signaling: transport, auth, operations."""

import asyncio
import base64
import hashlib
import os
import secrets
import socket
import ssl
import time
import logging
from dataclasses import dataclass

from . import const as C
from . import media_handler as media
from . import sip_locate as locate
from .backoff import reconnect_delay
from .runtime import RuntimeConfig
from .sip_parser import (
    ParsedMessage,
    addr_uri,
    call_id_key,
    granted_expiry,
    header_params,
    header_values,
    parse_message,
    reason_cause,
    response_keys,
    tag_of,
    transaction_key,
)
from .system_messages import summarize_body

_LOGGER = logging.getLogger(__name__)

# Set once by the hub at start-up. The integration declares
# single_config_entry, so one module-global config is correct.
CFG: RuntimeConfig | None = None


def configure(cfg: RuntimeConfig) -> None:
    """Install the runtime configuration for this SIP client."""
    global CFG
    CFG = cfg


# ─── Broadcast callback (set by hub) ────────────────────────────────
_broadcast = None


def init(broadcast_fn):
    global _broadcast
    _broadcast = broadcast_fn


async def broadcast(msg_type, msg):
    if _broadcast:
        await _broadcast(msg_type, msg)


# ─── State ──────────────────────────────────────────────────────────
reader = None
writer = None
lock = None
registered = False
in_call = False
calling = False
cseq_counter = 0
local_tag = None
MY_IP = None

# Registration lifetime, tracked so `is_registered()` reflects what the
# registrar actually granted rather than a flag that only ever moves
# forward. `None` while there is no live registration.
registration_expiry: float | None = None
_reregister_task: asyncio.Task | None = None

# Set by connection_supervisor() on each connection attempt; used by
# request_reconnect() to interrupt a blocked read immediately instead of
# waiting for the socket to notice on its own.
_connection_lost: asyncio.Event | None = None

# State change callback — hub sets this to notify entities
_state_change_callback = None

call_state = {
    "call_id": None, "from_tag": None, "to_tag": None,
    "remote_contact": None, "remote_sdp": None, "original_target": None,
    # The dialog's route set (RFC 3261 §12.1): the Route headers every
    # request inside the call must carry. Empty outside a call.
    "route_set": (),
}

# Set when a hang-up arrives while `do_call` still owns the dialog, and
# honoured by `do_call` the moment the call is complete enough to end.
# `do_hangup` cannot act itself in that window — see its docstring.
hangup_requested = False


def _set_hangup_requested(val: bool) -> None:
    global hangup_requested
    hangup_requested = val

pending_transactions: dict[str, asyncio.Queue] = {}
incoming_requests: asyncio.Queue = None


def reset_state() -> None:
    """Forget every trace of a previous run of this module.

    The SIP layer is module state, so a Home Assistant reload leaves it
    exactly as the previous entry left it, while the socket that gave it
    meaning is gone. An `in_call` surviving a reload sticks the in-call
    sensor on, makes `stream_opened` refuse to place a call, and makes
    the hub decline the next real doorbell press as the echo of a call
    that no longer exists — and nothing can clear it, because the dialog
    that would have produced a BYE died with the socket. `async_start`
    calls this before anything else.
    """
    global reader, writer, lock, registered, in_call, calling
    global cseq_counter, local_tag, registration_expiry, hangup_requested
    global _state_change_callback, _broadcast

    _cancel_reregister()
    # Drop the callbacks first. They still point at the torn-down hub
    # and its removed entities; the new hub installs its own straight
    # after this returns.
    _state_change_callback = None
    _broadcast = None
    reader = None
    writer = None
    lock = None
    registered = False
    in_call = False
    calling = False
    cseq_counter = 0
    local_tag = None
    registration_expiry = None
    hangup_requested = False
    call_state.update(call_id=None, from_tag=None, to_tag=None,
                      remote_contact=None, remote_sdp=None,
                      original_target=None, route_set=())
    pending_incoming.update(
        active=False, cid=None, from_hdr=None, to_hdr=None, cseq=None,
        via_block=None, my_tag=None, caller_uri=None, caller_tag=None,
        call_ids=(), record_route=(), contact=None,
        body=None)
    pending_transactions.clear()


def _open_transaction(branch: str, seq: int, method: str, call_id: str) -> str:
    """Register a transaction and return its key."""
    key = transaction_key(branch, seq, method)
    queue: asyncio.Queue = asyncio.Queue()
    pending_transactions[key] = queue
    pending_transactions[call_id_key(call_id, seq, method)] = queue
    return key


def _close_transaction(key: str, call_id: str, seq: int, method: str) -> None:
    """Forget a transaction and its Call-ID fallback."""
    pending_transactions.pop(key, None)
    pending_transactions.pop(call_id_key(call_id, seq, method), None)


def set_state_callback(cb):
    """Set callback that fires on registered/in_call changes."""
    global _state_change_callback
    _state_change_callback = cb


def _notify_state_change():
    """Notify hub that SIP state changed."""
    if _state_change_callback:
        try:
            _state_change_callback()
        except Exception:
            _LOGGER.exception("State change callback error")


def _set_registered(val: bool):
    global registered
    if registered != val:
        registered = val
        _notify_state_change()


def _set_in_call(val: bool):
    global in_call
    if in_call != val:
        in_call = val
        _notify_state_change()


def _set_calling(val: bool):
    global calling
    calling = val


def is_registered() -> bool:
    """True only while a registration granted by the registrar is valid."""
    if not registered or registration_expiry is None:
        return False
    return time.monotonic() < registration_expiry


def get_local_ip():
    """Detect the local IP by opening a UDP socket toward the proxy."""
    for host, port in ((CFG.proxy_host, CFG.proxy_port),
                       (CFG.local_proxy, CFG.local_sip_port)):
        if not host:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, port))
            ip = sock.getsockname()[0]
            _LOGGER.debug("Detected local IP %s", ip)
            return ip
        except OSError:
            continue
        finally:
            sock.close()
    _LOGGER.warning("Could not determine the local IP address")
    return "0.0.0.0"


def _gen(prefix="z9hG4bK"):
    """A fresh Via branch, From tag or Call-ID token.

    These identify a dialog, so a predictable one lets anything that can
    reach the proxy guess the identifiers of a call in progress.
    `random` is a Mersenne Twister seeded from a 32-bit-ish entropy pool
    and the old `randint(100000, 9999999)` offered about 2^23 values;
    `secrets` is the CSPRNG the rest of this component already uses.
    """
    return f"{prefix}{secrets.token_hex(8)}"


def _next_cseq():
    global cseq_counter
    cseq_counter += 1
    return cseq_counter


def record_route_entries(raw: str) -> list[str]:
    """Every Record-Route entry of a message, in the order it carries them.

    One header may hold several comma-separated entries; a comma inside
    `<...>` belongs to the URI and does not split.
    """
    entries: list[str] = []
    for value in header_values(raw, "record-route"):
        depth, start = 0, 0
        for pos, char in enumerate(value):
            if char == "<":
                depth += 1
            elif char == ">":
                depth -= 1
            elif char == "," and depth == 0:
                entries.append(value[start:pos].strip())
                start = pos + 1
        entries.append(value[start:].strip())
    return [entry for entry in entries if entry]


def _route_lines() -> str:
    """The Route headers for a request inside the current call.

    The cloud relays a call to the panel through several hops and names
    them in Record-Route. A request inside the call has to retrace them:
    with only the static route to the cloud proxy, the keyframe requests
    and the BYE reached the proxy and went no further, so video waited
    for the panel's own next keyframe and every hang-up sat out its
    five-second timeout. Outside a dialog, or when the far end recorded
    no route, the static route is still right.
    """
    route_set = call_state.get("route_set") or ()
    if not route_set:
        return f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
    return "".join(f"Route: {entry}\r\n" for entry in route_set)


def _contact_uri(value: str) -> str:
    """The URI of a Contact header value, without its parameters."""
    if "<" in value and ">" in value:
        return value[value.index("<") + 1:value.index(">")]
    return value.split(";", 1)[0].strip()


def _content_length(body: str) -> int:
    """The byte count of a SIP body, which is what Content-Length means.

    `send` encodes the whole message as UTF-8, so counting characters
    under-declares any body that is not pure ASCII. The proxy then frames
    the surplus bytes as the start of the next request and the stream is
    corrupt from that point on. `door_command` is user-supplied, so a
    single accented character used to be enough — no attacker required.
    """
    return len(body.encode())


# ─── Digest Auth ────────────────────────────────────────────────────

def _compute_ha1(realm):
    """HA1 for the realm the server named.

    A realm other than the configured domain is answered rather than
    refused. That does hand a proxy a chosen-realm
    `MD5(user:realm:password)` — offline-crackable, though the password
    itself never leaves — but the proxy is reached over a TLS connection
    whose certificate is verified (see `_create_ssl_context`), so
    obtaining that hash already requires being the real proxy or holding
    a valid certificate for it. Refusing instead would break every
    installation whose registrar names a realm that is not its domain,
    which cannot be checked from here. See ARCHITECTURE.md.
    """
    if realm == CFG.sip_domain:
        return CFG.sip_ha1
    return hashlib.md5(f"{CFG.sip_user}:{realm}:{CFG.sip_password}".encode()).hexdigest()


def _digest_resp(method, uri, nonce, realm=None, qop=None, nc=None, cnonce=None):
    ha1 = _compute_ha1(realm or CFG.sip_domain)
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop == "auth":
        return hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


def _make_auth(method, uri, challenge):
    """Build the digest credentials that answer one challenge.

    Only MD5 is implemented, which is what the Vimar cloud challenges
    with. A server asking for anything else gets an MD5 response it will
    reject — that is a visible failure, and it is logged here, rather
    than the silent wrong answer the caller cannot distinguish from a
    bad password.

    `nc` is fixed at 1 because every request this client sends opens its
    own transaction and is challenged afresh; the nonce is never reused
    across two requests. A registrar that both reuses a nonce and
    enforces a monotonic `nc` would reject the second request inside one
    nonce's lifetime, and would need real nonce bookkeeping here.
    """
    p = header_params(challenge)
    nonce = p.get("nonce", "")
    realm = p.get("realm", CFG.sip_domain)
    opaque = p.get("opaque", "")
    algorithm = (p.get("algorithm") or "MD5").strip()
    if algorithm.upper() not in ("MD5", "MD5-SESS"):
        _LOGGER.warning(
            "The server asked for digest algorithm %s, which this client "
            "cannot compute; answering with MD5, which it will reject",
            algorithm)
    # `qop` is a comma-separated list of what the server accepts. A bare
    # substring test also matched `auth-int`, and the code then sent
    # `qop=auth` with an auth-style ha2: a wrong response, and a silent
    # downgrade of the integrity mode the server had asked for.
    qop_values = [v.strip().strip('"') for v in p.get("qop", "").split(",")]
    nc = "00000001"
    cnonce = secrets.token_hex(8)
    if "auth" in qop_values:
        resp = _digest_resp(method, uri, nonce, realm, "auth", nc, cnonce)
        hdr = (f'Digest username="{CFG.sip_user}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        resp = _digest_resp(method, uri, nonce, realm)
        hdr = (f'Digest username="{CFG.sip_user}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5')
    if opaque:
        hdr += f', opaque="{opaque}"'
    return hdr


def _challenge_of(msg: ParsedMessage) -> tuple[str, str]:
    """Return one challenge and the request header that answers it.

    A 407 is the proxy challenging, and is answered with
    `Proxy-Authorization`; a 401 is the endpoint challenging, and is
    answered with `Authorization`. Emitting `Proxy-Authorization` for a
    401 — which is what this used to do for every MESSAGE and every
    INVITE — means the server ignores the credentials and rejects the
    retry identically. For a door command that is the whole failure: the
    press is accepted, nothing happens, and the log says only 401.
    """
    if msg.code == 407:
        return msg.headers.get("proxy-authenticate", ""), "Proxy-Authorization"
    return msg.headers.get("www-authenticate", ""), "Authorization"


# ─── Transport ──────────────────────────────────────────────────────

def _create_ssl_context():
    """The verified TLS context every SIP connection uses.

    A missing CA file used to disable certificate validation outright,
    on the one socket that carries the door command and the digest
    response, with no log line and no symptom. A component that cannot
    verify the proxy refuses to talk to it instead: `__init__.py` checks
    for the same file at setup and raises `ConfigEntryNotReady`, and
    this raise covers the file going away afterwards.

    `load_verify_locations` on top of `create_default_context()` adds the
    Vimar CA to the system roots rather than replacing them, so this is
    not certificate pinning: any publicly trusted certificate for the
    proxy host also validates. That is deliberate — see ARCHITECTURE.md.
    """
    if not os.path.exists(C.CA_PATH):
        raise FileNotFoundError(
            f"The Vimar CA certificate is missing from {C.CA_PATH}. "
            "Reinstall the integration: without it the proxy's "
            "certificate cannot be verified, and this connection carries "
            "the door command.")
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(C.CA_PATH)
    return ctx


async def _connect_targets() -> list[locate.Target]:
    """Where this connection attempt may open its socket, in order.

    The cloud proxy is a SIP domain and is located through SRV, afresh on
    every attempt. The local panel is a LAN address and is dialled as
    configured: SRV is the cloud's arrangement, not the panel's.
    """
    if not CFG.locate_by_srv:
        return [locate.Target(CFG.proxy_host, CFG.proxy_port)]
    return await locate.resolve_targets(
        CFG.proxy_host, CFG.proxy_port,
        port_override=CFG.proxy_port_override)


async def connect():
    global reader, writer, lock, MY_IP
    loop = asyncio.get_running_loop()
    ctx = await loop.run_in_executor(None, _create_ssl_context)
    targets = await _connect_targets()

    async def _open(target: locate.Target):
        # Only the TCP destination follows SRV. `server_hostname` stays
        # the name from the QR, so SNI and the certificate hostname check
        # are made against the domain the certificate is issued for, never
        # against whichever server SRV happened to pick.
        return await asyncio.open_connection(
            target.host, target.port, ssl=ctx, server_hostname=CFG.sni,
            happy_eyeballs_delay=C.SIP_HAPPY_EYEBALLS_DELAY)

    target, (reader, writer) = await locate.open_first(
        targets, _open, timeout=C.SIP_CONNECT_TIMEOUT, domain=CFG.sni)
    lock = asyncio.Lock()
    # The address this connection actually leaves from is the one Via,
    # Contact and the SDP must carry. Guessing it once at startup through
    # a DNS lookup failed whenever DNS was not ready yet, and left the
    # client advertising 0.0.0.0 for the life of the entry. The SDP says
    # IN IP4, so an IPv6 source keeps the previous value.
    sockname = writer.get_extra_info("sockname")
    if sockname and "." in sockname[0] and ":" not in sockname[0]:
        MY_IP = sockname[0]
    _LOGGER.info("SIP TLS connection established with %s", target)


async def send(msg: str):
    first_line = msg.split("\r\n", 1)[0]
    _LOGGER.debug("[SIP >>>] %s", first_line)
    try:
        async with lock:
            writer.write(msg.encode())
            await writer.drain()
    except Exception as e:
        _LOGGER.error("[SIP >>>] send failed: %s", e)
        raise


# ─── Connection supervisor ──────────────────────────────────────────

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
    are all treated the same way. The eleven-day outage this replaces
    began as a transient DNS failure that the old five-attempt reconnect
    could not ride out.
    """
    global _connection_lost
    _connection_lost = asyncio.Event()
    attempt = 0

    try:
        while True:
            connected_at: float | None = None
            reader_task: asyncio.Task | None = None
            try:
                await connect()
                # The reader has to be running before the first REGISTER.
                # It is the only thing that reads the socket and hands each
                # response to the transaction waiting for it; started after
                # do_register, as it once was, the registrar's answer sat
                # unread in the socket buffer, every REGISTER timed out and
                # was reported as refused, and no installation could ever
                # register.
                reader_task = asyncio.create_task(_reader_loop())
                if not await do_register():
                    raise ConnectionError("registration was refused")
                connected_at = time.monotonic()
                await reader_task
                raise ConnectionError("connection closed by the server")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure means retry
                # Stop the dead connection's reader before backing off, or
                # it would keep dispatching messages from a connection this
                # supervisor has already given up on.
                await _stop_reader(reader_task)
                reader_task = None
                _clear_registration()
                await _abandon_call()
                if (connected_at is not None
                        and time.monotonic() - connected_at
                        >= C.STABLE_CONNECTION_SECONDS):
                    # The connection was genuinely healthy for a while, so this
                    # is a fresh problem rather than a continuation. Resetting
                    # on every accepted REGISTER instead would let a registrar
                    # that accepts and then immediately drops us cycle roughly
                    # every two seconds, forever, with the ladder never engaging.
                    attempt = 0
                attempt += 1
                delay = reconnect_delay(attempt)
                _LOGGER.warning(
                    "SIP connection unavailable (%s); retrying in %.0fs "
                    "(attempt %d)", err, delay, attempt)
                await asyncio.sleep(delay)
            finally:
                # Cancellation skips the handler above; this covers it.
                await _stop_reader(reader_task)
    finally:
        # The supervisor only ever exits via cancellation (hub.async_stop
        # tearing the task down for HA unload). This must clear the full
        # registration state, not just cancel the timer: without it, a
        # pending re-register task scheduled by _accept_registration would
        # keep sleeping past shutdown and then reconnect on its own — an
        # orphaned connection outliving the integration it belongs to —
        # and `registration_expiry` would keep is_registered() reporting a
        # live registration for up to its full lifetime after the socket
        # is closed, since neither hub.async_stop() nor __init__.py clears
        # it themselves.
        _clear_registration()


async def _stop_reader(task: asyncio.Task | None) -> None:
    """Stop a connection's reader and wait until it has really stopped.

    Awaited rather than fire-and-forget, so the old reader is gone before
    the next connect() replaces the socket, and whatever exception it
    ended with is retrieved instead of reported by asyncio as never
    retrieved. The supervisor's own cancellation is never swallowed.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:  # noqa: BLE001 - the connection is over either way
        pass


async def _reader_loop() -> None:
    """Read and dispatch SIP messages until the connection ends."""
    _connection_lost.clear()
    buf = b""
    while not _connection_lost.is_set():
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=30)
        except asyncio.TimeoutError:
            # RFC 5626 CRLF keepalive, so the proxy does not drop us.
            async with lock:
                writer.write(b"\r\n\r\n")
                await writer.drain()
            continue
        if not chunk:
            return
        buf += chunk
        buf = await _dispatch_buffer(buf)


# Largest SIP body this client will frame. Most of what it receives is
# an SDP offer or answer, a few kilobytes, but the indoor unit sends its
# video-message mailbox as one MESSAGE whose body is the whole SQLite
# file in base64 (`voicemail.py`), so the ceiling is sized for that: the
# largest mailbox `voicemail` accepts, base64-encoded, with room to
# spare. The value comes off the wire, so without a ceiling a peer can
# name a huge length and make the reader buffer until the host runs out
# of memory.
MAX_BODY_BYTES = 3 * 1024 * 1024


async def _dispatch_buffer(buf: bytes) -> bytes:
    """Parse complete SIP messages out of `buf`, dispatch them, and
    return the unconsumed remainder."""
    while b"\r\n\r\n" in buf:
        hdr_end = buf.index(b"\r\n\r\n") + 4
        hdr_text = buf[:hdr_end].decode(errors="replace")
        cl = 0
        for line in hdr_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    cl = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        # Both bounds are on a value the peer chose. A negative length
        # made `total` smaller than `hdr_end` and re-sliced part of the
        # header back into the buffer, where it was parsed again as a
        # message of its own.
        if cl < 0 or cl > MAX_BODY_BYTES:
            _LOGGER.warning(
                "Dropping the SIP connection: a peer declared a "
                "Content-Length of %d", cl)
            request_reconnect()
            return b""
        total = hdr_end + cl
        if len(buf) < total:
            break
        raw = buf[:total].decode(errors="replace")
        buf = buf[total:]

        msg = parse_message(raw)
        _LOGGER.debug("[SIP <<<] %s", msg.start_line)

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
    return buf


async def _wait_final(key: str, call_id: str, seq: int, method: str,
                       timeout: float = 15) -> list[str]:
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
        _close_transaction(key, call_id, seq, method)
    return results


# ─── SDP ────────────────────────────────────────────────────────────

def build_sdp():
    """Offer PCMU audio and H.264 video over SAVP.

    The two `a=crypto` keys describe the streams this client would send.
    Home Assistant has no talk-back path to the panel, but it does send
    silent audio, or the far end ends the call after about ten seconds.
    That audio is encrypted under the audio key offered here. The video
    key is never used, but SAVP requires the attribute.
    """
    sid = str(int(time.time()))
    audio_crypto_key = base64.b64encode(os.urandom(30)).decode()
    video_crypto_key = base64.b64encode(os.urandom(30)).decode()
    # The audio key is kept: the media layer sends silence under it.
    media.local_audio_key = audio_crypto_key
    return (
        f"v=0\r\n"
        f"o=- {sid} {sid} IN IP4 {MY_IP}\r\n"
        f"s=Talk\r\n"
        f"c=IN IP4 {MY_IP}\r\n"
        f"b=AS:512\r\n"
        f"t=0 0\r\n"
        f"a=rtcp-xr:rcvr-rtt=all:10000 stat-summary=loss,dup,jitt,TTL voip-metrics\r\n"
        f"m=audio {CFG.rtp_audio_port} RTP/SAVP 0 8 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-15\r\n"
        f"a=ptime:20\r\n"
        f"a=sendrecv\r\n"
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{audio_crypto_key}\r\n"
        f"m=video {CFG.rtp_video_port} RTP/SAVP 96\r\n"
        f"b=AS:256\r\n"
        f"a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1\r\n"
        f"a=rtcp-fb:96 ccm fir\r\n"
        f"a=rtcp-fb:96 nack\r\n"
        f"a=rtcp-fb:96 nack pli\r\n"
        f"a=sendrecv\r\n"
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{video_crypto_key}\r\n"
    )


def parse_sdp(sdp_text):
    result = {"audio": {}, "video": {}, "conn": ""}
    m = None
    for line in sdp_text.split("\n"):
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            ip = line.split()[-1]
            if m:
                result[m]["ip"] = ip
            else:
                result["conn"] = ip
        elif line.startswith("m=audio"):
            m = "audio"
            parts = line.split()
            result["audio"]["port"] = int(parts[1])
        elif line.startswith("m=video"):
            m = "video"
            parts = line.split()
            result["video"]["port"] = int(parts[1])
        elif line.startswith("a=rtpmap:") and m:
            result[m].setdefault("rtpmap", []).append(line)
        elif line.startswith("a=fmtp:") and m:
            result[m].setdefault("fmtp", []).append(line)
        elif line.startswith("a=crypto:") and m:
            parts = line.split()
            for p in parts:
                if p.startswith("inline:"):
                    result[m]["crypto_key"] = p[7:]
                    break
    for section in ("audio", "video"):
        if section in result and "ip" not in result[section]:
            result[section]["ip"] = result["conn"]
    return result


# ─── Operations ─────────────────────────────────────────────────────

REGISTER_EXPIRY_SAFETY = 0.5  # re-register at half the granted lifetime


def _accept_registration(msg: ParsedMessage) -> bool:
    """Record a successful registration and schedule the refresh.

    Returns False when the registrar granted no lifetime at all: a 200 with
    `expires=0` means it has de-registered us, and scheduling a refresh at
    half of zero would re-REGISTER in a tight loop for as long as the
    registrar kept answering that way.
    """
    global registration_expiry
    granted = granted_expiry(msg, CFG.sip_user, C.DEFAULT_REGISTER_EXPIRY)
    if granted <= 0:
        _LOGGER.warning(
            "Registrar granted a zero lifetime; treating as not registered")
        _clear_registration()
        return False
    granted = max(granted, C.MIN_REGISTER_EXPIRY)
    registration_expiry = time.monotonic() + granted
    _set_registered(True)
    _LOGGER.info("SIP registered for %ds", granted)
    _schedule_reregister(granted * REGISTER_EXPIRY_SAFETY)
    return True


def _schedule_reregister(delay: float) -> None:
    """Re-register before the current registration expires."""
    global _reregister_task
    _cancel_reregister()
    _reregister_task = asyncio.create_task(_reregister_after(delay))


def _cancel_reregister() -> None:
    """Cancel a pending refresh. Says nothing about whether we are registered."""
    global _reregister_task
    if _reregister_task is not None:
        _reregister_task.cancel()
        _reregister_task = None


def _clear_registration() -> None:
    """Record that the registration is gone, and stop refreshing it.

    Keep this separate from `_cancel_reregister`. Folding the two together
    is how the first version of this code wiped `registration_expiry`
    immediately after setting it — `_schedule_reregister` begins by
    cancelling the previous timer — leaving `is_registered()` permanently
    False and the connectivity sensor stuck off.
    """
    global registration_expiry
    _cancel_reregister()
    registration_expiry = None
    _set_registered(False)


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


async def do_register():
    """REGISTER over the connection the supervisor owns.

    Never connects on its own. A socket opened here would have no
    reader — `_reader_loop` only runs inside `connection_supervisor` —
    so every response to this REGISTER would go unread, and the socket
    would be orphaned the moment the supervisor opened its own.
    """
    if not writer or writer.is_closing():
        _LOGGER.warning("REGISTER skipped: there is no SIP connection")
        return False

    global local_tag
    local_tag = _gen("")
    cid = _gen("reg-")
    uri = f"sip:{CFG.sip_domain}"

    def _msg(branch, seq, auth=None):
        contact_uri = f"sip:{CFG.sip_user}@{MY_IP}:{C.SIP_LOCAL_PORT};transport=tls"
        if CFG.push_token:
            contact_uri += (f";app-id={C.PN_APP_ID}"
                           f";pn-type={C.PN_TYPE}"
                           f";pn-tok={CFG.push_token}"
                           f";pn-msg-str=IM_MSG;pn-msg-snd=msg.caf"
                           f";pn-call-str=IC_MSG;pn-call-snd=notes_of_the_optimistic.caf"
                           f";q=0.00;domain-name={CFG.sip_domain}")
        contact = f"<{contact_uri}>"
        contact += f';+sip.instance="<urn:uuid:{CFG.device_uuid}>"'
        contact += f";expires={'5184000' if CFG.push_token else '3600'}"
        m = (f"REGISTER {uri} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
             f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <sip:{CFG.sip_user}@{CFG.sip_domain}>\r\n"
             f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={local_tag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} REGISTER\r\n"
             f"Contact: {contact}\r\n"
             f"User-Agent: {CFG.user_agent}\r\n"
             f"Mobile-IMEI: {CFG.device_id}\r\n"
             f"MyName: {C.MY_NAME}\r\n"
             f"Supported: replaces,outbound,gruu\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n")
        if auth:
            m += f"Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    branch = _gen()
    seq = _next_cseq()
    key = _open_transaction(branch, seq, "REGISTER", cid)
    await send(_msg(branch, seq))
    responses = await _wait_final(key, cid, seq, "REGISTER")

    for raw in responses:
        msg = parse_message(raw)
        if msg.code == 401:
            ch = msg.headers.get("www-authenticate", "")
            if not ch:
                return False
            auth = _make_auth("REGISTER", uri, ch)
            branch2 = _gen()
            seq2 = _next_cseq()
            key2 = _open_transaction(branch2, seq2, "REGISTER", cid)
            await send(_msg(branch2, seq2, auth=auth))
            for raw2 in await _wait_final(key2, cid, seq2, "REGISTER"):
                msg2 = parse_message(raw2)
                if msg2.code == 200:
                    return _accept_registration(msg2)
            return False
        elif msg.code == 200:
            return _accept_registration(msg)
    return False


# Returned as the message of `do_system_message` when no final response
# ever arrived. It is not a failure: the request may well have reached
# the panel and only its 200 OK been lost, so a caller must never resend
# on this. `hub.async_door` depends on the distinction — a resent door
# command pulses the relay a second time.
NO_RESPONSE = "Timed out"


async def do_system_message(target_uri, body_text, extra_headers=None):
    """Send one MESSAGE and report how it ended.

    Returns `(True, "OK (2xx)")`, or `(False, reason)` where `reason` is
    `NO_RESPONSE` when nothing final came back and a description of the
    response otherwise.
    """
    if not is_registered():
        # Never the body: it can be a door command, a call ID or a
        # setting, and the log is readable by anyone the user shares it
        # with. Its kind is what a warning needs.
        _LOGGER.warning("Cannot send a system message (%s) to %s: not registered",
                        summarize_body(body_text), target_uri)
        return False, "Not registered"
    _LOGGER.debug("Sending a system message (%s) to %s",
                  summarize_body(body_text), target_uri)
    ftag = _gen("")
    cid = _gen("sys-")

    def _msg(branch, seq, auth=None, auth_header="Proxy-Authorization"):
        m = (f"MESSAGE {target_uri} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
             f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} MESSAGE\r\n"
             f"Contact: <sip:{CFG.sip_user}@{MY_IP}:{C.SIP_LOCAL_PORT};transport=tls>\r\n"
             f"User-Agent: {CFG.user_agent}\r\n"
             f"Mobile-IMEI: {CFG.device_id}\r\n"
             f"MyName: {C.MY_NAME}\r\n")
        if extra_headers:
            for k, v in extra_headers.items():
                m += f"{k}: {v}\r\n"
        if auth:
            m += f"{auth_header}: {auth}\r\n"
        m += (f"Content-Type: text/plain\r\n"
              f"Content-Length: {_content_length(body_text)}\r\n\r\n{body_text}")
        return m

    branch = _gen()
    seq = _next_cseq()
    key = _open_transaction(branch, seq, "MESSAGE", cid)
    await send(_msg(branch, seq))
    for raw in await _wait_final(key, cid, seq, "MESSAGE", timeout=15):
        msg = parse_message(raw)
        _LOGGER.debug("do_system_message: response %s for %s", msg.code, target_uri)
        if msg.code and msg.code < 200:
            continue
        if msg.code in (401, 407):
            ch, auth_header = _challenge_of(msg)
            if not ch:
                return False, f"Empty authentication challenge ({msg.code})"
            auth = _make_auth("MESSAGE", target_uri, ch)
            branch2 = _gen()
            seq2 = _next_cseq()
            key2 = _open_transaction(branch2, seq2, "MESSAGE", cid)
            await send(_msg(branch2, seq2, auth=auth, auth_header=auth_header))
            for raw2 in await _wait_final(key2, cid, seq2, "MESSAGE", timeout=15):
                msg2 = parse_message(raw2)
                _LOGGER.debug("do_system_message: auth response %s for %s", msg2.code, target_uri)
                if msg2.code and 200 <= msg2.code < 300:
                    return True, f"OK ({msg2.code})"
                if msg2.code and msg2.code >= 300:
                    return False, f"Rejected with {msg2.code}"
            return False, NO_RESPONSE
        if msg.code and 200 <= msg.code < 300:
            return True, f"OK ({msg.code})"
        if msg.code and msg.code >= 300:
            return False, f"Rejected with {msg.code}"
    return False, NO_RESPONSE


async def do_call(target=None):
    """INVITE a SIP target (default: intercom entrance panel 55001)."""
    if not is_registered():
        _LOGGER.error("do_call: NOT registered")
        return False, "Not registered"
    if in_call or calling:
        _LOGGER.error("do_call: already in call/calling")
        return False, "Already in a call"

    _set_calling(True)
    # A previous call that never reached `_end_call_locally` — an INVITE
    # the panel rejected after the user had already pressed Hang up —
    # must not end this one before it starts.
    _set_hangup_requested(False)
    key: str | None = None
    cur_seq: int | None = None
    cid = _gen("call-")

    # Everything from here on runs with `calling` already set and, once
    # the transaction is opened below, a transaction pending too. A raise
    # anywhere in this block (build_sdp, send, broadcast, parse_sdp,
    # media.setup_media, send_keyframe_request, _make_auth) must not
    # strand either one — the finally below closes whichever transaction
    # is current and always clears `calling` unless a call is now up. The
    # authenticated-retry branch closes the first transaction itself
    # before opening the second, so each transaction this call opens is
    # still closed exactly once.
    try:
        target_uri = target or CFG.panel_uri(CFG.default_panel.address)
        _LOGGER.info("do_call: target=%s", target_uri)
        ftag = _gen("")
        sdp = build_sdp()
        call_state["call_id"] = cid
        call_state["from_tag"] = ftag
        call_state["original_target"] = target_uri

        vimar_callid = secrets.token_hex(5)

        def _inv(branch, seq, auth=None, auth_header="Proxy-Authorization"):
            m = (f"INVITE {target_uri} SIP/2.0\r\n"
                 f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
                 f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
                 f"Max-Forwards: 70\r\n"
                 f"To: <{target_uri}>\r\n"
                 f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={ftag}\r\n"
                 f"Call-ID: {cid}\r\n"
                 f"CSeq: {seq} INVITE\r\n"
                 f"Contact: <sip:{CFG.sip_user}@{MY_IP}:{C.SIP_LOCAL_PORT};transport=tls>"
                 f';+sip.instance="<urn:uuid:{CFG.device_uuid}>"\r\n'
                 f"User-Agent: {CFG.user_agent}\r\n"
                 f"Supported: replaces,outbound,gruu,timer\r\n"
                 f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
                 f"Session-Expires: 600;refresher=uas\r\n"
                 f"Min-SE: 90\r\n")
            if auth:
                m += f"{auth_header}: {auth}\r\n"
            m += (f"Mobile-IMEI: {CFG.device_id}\r\n"
                  f"MyName: {C.MY_NAME}\r\n"
                  f"X-Call-ID: {vimar_callid}\r\n"
                  f"Content-Type: application/sdp\r\n"
                  f"Content-Length: {_content_length(sdp)}\r\n\r\n{sdp}")
            return m

        def _ack(to_tag, seq):
            branch = _gen()
            to_hdr = f"<{target_uri}>"
            if to_tag:
                to_hdr += f";tag={to_tag}"
            ack_uri = call_state.get("remote_contact") or target_uri
            return (f"ACK {ack_uri} SIP/2.0\r\n"
                    f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
                    f"{_route_lines()}"
                    f"Max-Forwards: 70\r\n"
                    f"To: {to_hdr}\r\n"
                    f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={ftag}\r\n"
                    f"Call-ID: {cid}\r\n"
                    f"CSeq: {seq} ACK\r\n"
                    f"Content-Length: 0\r\n\r\n")

        branch = _gen()
        cur_seq = _next_cseq()
        key = _open_transaction(branch, cur_seq, "INVITE", cid)
        await send(_inv(branch, cur_seq))
        await broadcast("log", "INVITE sent")

        queue = pending_transactions[key]
        deadline = time.monotonic() + 45

        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=3)
            except asyncio.TimeoutError:
                continue

            msg = parse_message(raw)
            ttag = tag_of(msg.headers.get("to", ""))
            _LOGGER.debug("do_call: response %s (body=%dB)", msg.code, len(msg.body) if msg.body else 0)

            if msg.code in (100, 180, 183):
                if msg.code == 183 and msg.body:
                    call_state["remote_sdp"] = parse_sdp(msg.body)
                continue

            if msg.code in (401, 407):
                await send(_ack(ttag, cur_seq))
                ch, auth_header = _challenge_of(msg)
                if not ch:
                    return False, f"Empty authentication challenge ({msg.code})"
                auth = _make_auth("INVITE", target_uri, ch)
                _close_transaction(key, cid, cur_seq, "INVITE")
                branch = _gen()
                cur_seq = _next_cseq()
                key = _open_transaction(branch, cur_seq, "INVITE", cid)
                queue = pending_transactions[key]
                await send(_inv(branch, cur_seq, auth=auth,
                                auth_header=auth_header))
                continue

            if 200 <= msg.code < 300:
                call_state["to_tag"] = ttag
                call_state["remote_contact"] = _contact_uri(
                    msg.headers.get("contact", ""))
                # The caller's route set is the Record-Route of the 2xx,
                # reversed (RFC 3261 §12.1.2).
                call_state["route_set"] = tuple(
                    reversed(record_route_entries(raw)))
                await send(_ack(ttag, cur_seq))

                if hangup_requested:
                    # The user pressed Hang up while this INVITE was in
                    # flight. `do_hangup` deferred to us then, and only
                    # now is the dialog complete enough to end: the
                    # To-tag and the remote Contact are set and the ACK
                    # has gone, so the BYE below is a valid one. Media is
                    # never started, and `do_hangup` runs the whole local
                    # teardown including clearing the request.
                    _set_in_call(True)
                    await do_hangup()
                    return False, "Hung up while the call was being set up"

                if msg.body:
                    remote = parse_sdp(msg.body)
                    call_state["remote_sdp"] = remote
                    _LOGGER.debug("SDP: audio=%s video=%s", remote.get('audio', {}), remote.get('video', {}))
                    await media.setup_media(remote)

                _set_in_call(True)
                _set_calling(False)
                # The hub starts its keyframe requests on call_started.
                # Awaiting one here held the call's result back for as
                # long as the INFO and its authentication took.
                await broadcast("call_started", "Connected")
                return True, "Connected"

            if msg.code >= 300:
                _LOGGER.error("INVITE rejected: %d", msg.code)
                await send(_ack(ttag, cur_seq))
                reason = (msg.start_line.split(" ", 2)[2]
                          if msg.start_line.count(" ") >= 2 else str(msg.code))
                return False, f"{msg.code} {reason}"

        _LOGGER.error("INVITE timeout (45s) for %s", target_uri)
        return False, "Timed out (45s)"
    finally:
        if key is not None:
            _close_transaction(key, cid, cur_seq, "INVITE")
        if not in_call:
            _set_calling(False)


async def send_keyframe_request():
    """Ask the panel for a keyframe with a SIP INFO picture_fast_update.

    The cloud proxy challenges an INFO like any other request, so a 407
    is answered with credentials, as a MESSAGE is. Unanswered, the
    request never reached the panel and the video waited for the panel's
    own next keyframe.
    """
    if not in_call or not call_state["call_id"]:
        return
    info_target = call_state.get("remote_contact") or CFG.panel_uri(CFG.default_panel.address)
    to_uri = call_state.get("original_target") or CFG.panel_uri(CFG.default_panel.address)
    cid = call_state["call_id"]
    body = ('<?xml version="1.0" encoding="utf-8" ?>'
            '<media_control><vc_primitive><to_encoder>'
            '<picture_fast_update></picture_fast_update>'
            '</to_encoder></vc_primitive></media_control>')

    def _info(branch, seq, auth=None, auth_header="Proxy-Authorization"):
        m = (f"INFO {info_target} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
             f"{_route_lines()}"
             f"Max-Forwards: 70\r\n"
             f"To: <{to_uri}>;tag={call_state['to_tag']}\r\n"
             f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={call_state['from_tag']}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} INFO\r\n")
        if auth:
            m += f"{auth_header}: {auth}\r\n"
        m += (f"Content-Type: application/media_control+xml\r\n"
              f"Content-Length: {_content_length(body)}\r\n\r\n{body}")
        return m

    branch, seq = _gen(), _next_cseq()
    key = _open_transaction(branch, seq, "INFO", cid)
    await send(_info(branch, seq))
    for raw in await _wait_final(key, cid, seq, "INFO", timeout=3):
        msg = parse_message(raw)
        if msg.code in (401, 407):
            ch, auth_header = _challenge_of(msg)
            if not ch:
                return
            branch2, seq2 = _gen(), _next_cseq()
            key2 = _open_transaction(branch2, seq2, "INFO", cid)
            await send(_info(branch2, seq2,
                             auth=_make_auth("INFO", info_target, ch),
                             auth_header=auth_header))
            await _wait_final(key2, cid, seq2, "INFO", timeout=3)
    _LOGGER.debug("Sent INFO picture_fast_update (keyframe request)")


async def _end_call_locally() -> None:
    """Forget the current call and tell everyone it is over.

    The local half of ending a call is unconditional. A BYE that cannot
    leave the machine — the socket is gone, which is the normal state
    during the outage this rewrite exists for — still ends the call
    here. Leaving `in_call` True with no dialog behind it sticks the
    in-call sensor on, makes `stream_opened` refuse to place a call, and
    makes the hub decline every real doorbell press with a 603 as the
    echo of a call that no longer exists, until the entry is reloaded.
    """
    _set_calling(False)
    _set_in_call(False)
    _set_hangup_requested(False)
    call_state.update(call_id=None, from_tag=None, to_tag=None,
                      remote_contact=None, remote_sdp=None,
                      original_target=None, route_set=())
    try:
        await media.stop_media()
    except Exception:  # noqa: BLE001 - teardown must still reach the broadcast
        _LOGGER.debug("Stopping media after a call failed", exc_info=True)
    await broadcast("call_ended", "Call ended")


async def _abandon_call() -> None:
    """Drop the call state a dead connection took with it.

    The dialog lived on the socket that just went: no BYE can be sent
    for it and none will ever arrive. Only `_clear_registration` used to
    run here, so a connection lost mid-call left `in_call` True with
    nothing able to clear it.
    """
    if not (in_call or calling or call_state["call_id"]
            or pending_incoming["active"]):
        return
    _LOGGER.info("The SIP connection went while a call was up; "
                 "ending that call locally")
    pending_incoming["active"] = False
    try:
        await _end_call_locally()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the supervisor must keep retrying
        _LOGGER.debug("Clearing the call state after a connection loss failed",
                      exc_info=True)


async def do_hangup():
    """End the current call: send a BYE if the socket still allows it,
    and clear the call locally whether or not it went out.

    `call_state` belongs to whoever is driving the dialog. While
    `do_call` is inside its INVITE transaction it is the owner, and this
    function only clears `calling` — see the guard below.
    """
    # Read before `_set_calling(False)` hides it: a call still being set
    # up is `calling` True, `in_call` False, with its Call-ID already in
    # `call_state`.
    in_setup = calling and not in_call and bool(call_state["call_id"])
    _set_calling(False)
    if not in_call or not call_state["call_id"]:
        if in_setup:
            # `do_call` is waiting on a 2xx that can take up to 45 s, and
            # it completes the dialog out of `call_state` — it sets the
            # To-tag and the remote Contact, and never re-sets the
            # Call-ID. Tearing down here would hand it a live call with
            # no identity: `send_keyframe_request` would early-return so
            # video could never recover, and every later hang-up — the
            # button, the five-minute limit, the unload — would take
            # this same branch and send no BYE, leaving the panel holding
            # the dialog and the account's single SIP registration
            # occupied.
            #
            # So the request is recorded rather than acted on, and
            # `do_call` honours it at its 2xx. Dropping it instead —
            # which is what this did — let the call establish and run to
            # the full five-minute limit, holding the account's single
            # registration while the hub declined every real doorbell
            # press as the echo of a call the user had already tried to
            # end. The setup window is up to 45 s and "open the camera,
            # see nothing, press Hang up" lands inside it.
            _set_hangup_requested(True)
            _LOGGER.info("Hang-up requested while the call is still being "
                         "set up; it will be honoured once the call is up")
            return
        # No dialog is being set up, so whatever is left in `call_state`
        # is stale and the local teardown is what clears it.
        await _end_call_locally()
        return

    cid = call_state["call_id"]
    ftag = call_state["from_tag"] or local_tag or _gen("")
    ttag = call_state["to_tag"] or ""

    target_uri = call_state.get("remote_contact") or CFG.panel_uri(CFG.default_panel.address)
    to_uri = call_state.get("original_target") or CFG.panel_uri(CFG.default_panel.address)
    to_hdr = f"<{to_uri}>"
    if ttag:
        to_hdr += f";tag={ttag}"

    branch = _gen()
    seq = _next_cseq()
    bye = (f"BYE {target_uri} SIP/2.0\r\n"
           f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
           f"{_route_lines()}"
           f"Max-Forwards: 70\r\n"
           f"To: {to_hdr}\r\n"
           f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: {seq} BYE\r\n"
           f"User-Agent: {CFG.user_agent}\r\n"
           f"Content-Length: 0\r\n\r\n")
    key = _open_transaction(branch, seq, "BYE", cid)
    try:
        await send(bye)
        await _wait_final(key, cid, seq, "BYE", timeout=5)
    finally:
        # `send` raises on a dead writer, and the caller swallows that.
        # The call still has to end here, or nothing ever ends it.
        _close_transaction(key, cid, seq, "BYE")
        await _end_call_locally()


# ─── Incoming SIP ───────────────────────────────────────────────────

pending_incoming = {
    "active": False, "cid": None, "from_hdr": None, "to_hdr": None,
    "cseq": None, "via_block": None, "my_tag": None,
    "caller_uri": None, "caller_tag": None, "body": None, "call_ids": (),
    "record_route": (), "contact": None,
}


async def handle_incoming_invite(raw):
    msg = parse_message(raw)
    from_hdr = msg.headers.get("from", "?")
    cid = msg.headers.get("call-id", "")
    via_block = _via_block(msg)
    to_hdr = msg.headers.get("to", "")
    cseq = msg.headers.get("cseq", "1 INVITE")

    caller_tag = tag_of(from_hdr)
    caller_uri = addr_uri(from_hdr)

    _LOGGER.info("Incoming INVITE from %s", caller_uri)

    my_tag = _gen("")

    # Every call identifier the INVITE carries, first one first. The SDK
    # reads the ring's call ID with linphone's `getCustomHeader("Call-ID")`,
    # which looks the name up among all the INVITE's headers and returns
    # the first match: the SIP Call-ID, unless a sender put its own
    # `Call-ID` ahead of it. The first one is what `C;<id>;ANSWERED` is
    # sent with; all of them (and an `X-Call-ID`) are what the unit's
    # notice from another device is matched against, since which one a
    # device reports cannot be verified from here.
    call_ids = tuple(dict.fromkeys(header_values(raw, "Call-ID", "X-Call-ID")))

    pending_incoming.update(
        active=True, cid=cid, from_hdr=from_hdr, to_hdr=to_hdr,
        cseq=cseq, via_block=via_block, my_tag=my_tag,
        caller_uri=caller_uri, caller_tag=caller_tag, body=msg.body,
        call_ids=call_ids,
        record_route=tuple(record_route_entries(raw)),
        contact=_contact_uri(msg.headers.get("contact", "")) or None,
    )

    await send(
        f"SIP/2.0 180 Ringing\r\n"
        f"{via_block}To: {to_hdr};tag={my_tag}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Contact: <sip:{CFG.sip_user}@{MY_IP}:{C.SIP_LOCAL_PORT};transport=tls>\r\n"
        f"Content-Length: 0\r\n\r\n")

    await broadcast("ring", f"Incoming call from {caller_uri}")


async def do_answer_incoming():
    if not pending_incoming["active"]:
        return False, "No incoming call"

    p = pending_incoming
    sdp = build_sdp()

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        # A 2xx copies the request's Record-Route (RFC 3261 §12.1.1), so
        # the hops that relayed the call stay in the dialog.
        + "".join(f"Record-Route: {entry}\r\n" for entry in p["record_route"])
        + f"Contact: <sip:{CFG.sip_user}@{MY_IP}:{C.SIP_LOCAL_PORT};transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {_content_length(sdp)}\r\n\r\n{sdp}")

    _set_in_call(True)
    call_state["call_id"] = p["cid"]
    call_state["from_tag"] = p["my_tag"]
    call_state["to_tag"] = p["caller_tag"]
    # Requests inside the call go to the caller's Contact along the
    # Record-Route of its INVITE, in order (RFC 3261 §12.1.1). The From
    # URI was used before, with the static route, and a BYE sent that
    # way was never answered.
    call_state["remote_contact"] = p["contact"] or p["caller_uri"]
    call_state["route_set"] = tuple(p["record_route"])
    call_state["original_target"] = p["caller_uri"]

    if p["body"]:
        remote = parse_sdp(p["body"])
        call_state["remote_sdp"] = remote
        _LOGGER.debug("Answer SDP: audio=%s video=%s", remote.get('audio'), remote.get('video'))
        await media.setup_media(remote)

    pending_incoming["active"] = False
    # The hub starts its keyframe requests on call_started.
    await broadcast("call_started", "Call established")
    return True, "Answered"


async def do_decline_incoming(busy: bool = False):
    """Refuse the pending INVITE.

    `busy` picks 486 Busy Here over 603 Decline, and the difference is
    not cosmetic. 603 is a *global* failure response: a forking proxy
    takes it as the whole invitation being refused and cancels the other
    branches, which in a Vimar plant means the indoor unit and the phone
    app stop ringing too. That is right for the PBX echoing back a call
    this client placed itself — we already have that call and want the
    echo gone. It is wrong for a real visitor arriving while we happen
    to be on a call: nobody refused them, this endpoint simply has no
    second line, which is exactly what 486 says, and the rest of the
    house keeps ringing.
    """
    if not pending_incoming["active"]:
        return

    status = "486 Busy Here" if busy else "603 Decline"
    p = pending_incoming
    await send(
        f"SIP/2.0 {status}\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Content-Length: 0\r\n\r\n")
    pending_incoming["active"] = False


def _via_block(msg: ParsedMessage) -> str:
    """Rebuild the Via stack of a request, for use in a response."""
    return "".join(f"Via: {via}\r\n" for via in msg.via_list)


async def handle_incoming_bye(raw):
    msg = parse_message(raw)
    cid = msg.headers.get("call-id", "")
    from_hdr = msg.headers.get("from", "")
    to_hdr = msg.headers.get("to", "")
    cseq = msg.headers.get("cseq", "1 BYE")

    # Only the dialog this BYE names ends. A late or duplicate BYE for a
    # call that is already over used to tear down whatever `call_state`
    # held at the time — including a call still being set up, which then
    # established with no Call-ID and could never be ended with a BYE.
    # The 200 OK is unconditional: the panel gets its answer either way.
    ours = bool(cid) and cid == call_state["call_id"]

    try:
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
    finally:
        # The panel has hung up. Whether our 200 OK reached it or the
        # socket died under us, the call is over on this side.
        if ours:
            await _end_call_locally()


async def handle_incoming_options(raw):
    msg = parse_message(raw)
    from_hdr = msg.headers.get("from", "")
    to_hdr = msg.headers.get("to", "")
    cid = msg.headers.get("call-id", "")
    cseq = msg.headers.get("cseq", "1 OPTIONS")
    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
        f"Content-Length: 0\r\n\r\n")


async def handle_incoming_cancel(raw):
    msg = parse_message(raw)
    cid = msg.headers.get("call-id", "")
    from_hdr = msg.headers.get("from", "")
    to_hdr = msg.headers.get("to", "")
    cseq = msg.headers.get("cseq", "1 CANCEL")

    _LOGGER.info("Incoming CANCEL for %s", cid[:24])

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    ours = bool(pending_incoming["active"] and pending_incoming["cid"] == cid)
    if ours:
        p = pending_incoming
        invite_cseq = p["cseq"]
        await send(
            f"SIP/2.0 487 Request Terminated\r\n"
            f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {invite_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
        pending_incoming["active"] = False

    # `Reason: SIP;cause=200` is the proxy saying another device took the
    # call — the SDK's "answered by others".
    await broadcast("ring_ended", RingEnded(
        call_id=cid, ours=ours,
        answered_elsewhere=reason_cause(msg.headers.get("reason", "")) == 200))


@dataclass(frozen=True)
class RingEnded:
    """The panel cancelled a ringing INVITE.

    `ours` is True when it was the INVITE this client had pending;
    `answered_elsewhere` when the CANCEL said another device answered.
    """

    call_id: str
    ours: bool
    answered_elsewhere: bool


@dataclass(frozen=True)
class InboundMessage:
    """A MESSAGE the indoor unit, or the cloud, sent to this client.

    `panda` is the `Panda` header — the message family (`blue` for
    status replies and notifications) — or None when there was none.
    `koala` names what a `grey` message carries (`mailbox.db`).
    """

    sender: str
    panda: str | None
    body: str
    koala: str | None = None

    def __repr__(self) -> str:
        # The body of a status reply carries the phonebook token. A
        # stray `%s` of this object must not put it in a log.
        return (f"InboundMessage(sender={self.sender!r}, panda={self.panda!r}, "
                f"koala={self.koala!r}, body=<{len(self.body)} chars>)")


async def handle_incoming_message(msg: ParsedMessage) -> None:
    """Acknowledge a MESSAGE, then hand it to the hub.

    The 200 OK goes first: the indoor unit retransmits a MESSAGE it has
    no answer for, and the hub's handling (a phonebook download, later)
    is no reason to keep it waiting. The hub still hears about it if the
    answer cannot be sent — the content arrived either way.

    Only the kinds of lines the body held are logged, never the body: a
    GET_INIT_STATUS_REPLY carries the password of the phonebook
    download, and this runs for every one.
    """
    from_hdr = msg.headers.get("from", "")
    to_hdr = msg.headers.get("to", "")
    msg_cid = msg.headers.get("call-id", "")
    msg_cseq = msg.headers.get("cseq", "1 MESSAGE")
    panda = msg.headers.get("panda")
    if panda is None or panda.strip().lower() == "blue":
        _LOGGER.debug("SIP MESSAGE received (%s)", summarize_body(msg.body))
    else:
        # A `grey` mailbox is a whole database in base64, possibly
        # wrapped into thousands of lines: its size says enough.
        _LOGGER.debug("SIP MESSAGE received (family %s, %d bytes)",
                      panda.strip(), len(msg.body))
    try:
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {msg_cid}\r\nCSeq: {msg_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
    finally:
        koala = msg.headers.get("koala")
        await broadcast("message", InboundMessage(
            sender=addr_uri(from_hdr) if from_hdr else "",
            panda=panda.strip() if panda else None,
            body=msg.body,
            koala=koala.strip() if koala else None))


async def request_processor():
    """Dispatch inbound SIP requests forever.

    Every iteration is isolated. MESSAGE and INFO are answered inline,
    and `send` raises when the writer is closing — a panel reboot in the
    instant an INFO is being answered used to take this loop down for
    good, leaving the supervisor happily reconnected and the doorbell
    silently dead until Home Assistant restarted.
    """
    while True:
        try:
            await _process_one_request()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad request must not end the loop
            _LOGGER.exception("Failed to process an incoming SIP request")


async def _process_one_request():
    """Handle exactly one inbound SIP request."""
    raw = await incoming_requests.get()
    msg = parse_message(raw)
    method = msg.method
    if method == "INVITE":
        asyncio.create_task(handle_incoming_invite(raw))
    elif method == "CANCEL":
        asyncio.create_task(handle_incoming_cancel(raw))
    elif method == "BYE":
        asyncio.create_task(handle_incoming_bye(raw))
    elif method == "OPTIONS":
        asyncio.create_task(handle_incoming_options(raw))
    elif method == "MESSAGE":
        await handle_incoming_message(msg)
    elif method == "INFO":
        from_hdr = msg.headers.get("from", "")
        to_hdr = msg.headers.get("to", "")
        info_cid = msg.headers.get("call-id", "")
        info_cseq = msg.headers.get("cseq", "1 INFO")
        await send(
            f"SIP/2.0 200 OK\r\n"
            f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
            f"Call-ID: {info_cid}\r\nCSeq: {info_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
    elif method != "ACK":
        _LOGGER.debug("Unhandled SIP request: %s", method)
