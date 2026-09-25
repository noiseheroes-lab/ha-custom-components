"""Vimar Intercom — SIP signaling: transport, auth, operations."""

import asyncio
import hashlib
import os
import random
import socket
import ssl
import string
import time
import logging

from . import const as C
from . import media_handler as media
from .backoff import reconnect_delay
from .runtime import RuntimeConfig
from .sip_parser import (
    ParsedMessage,
    addr_uri,
    call_id_key,
    granted_expiry,
    header_params,
    parse_message,
    response_keys,
    tag_of,
    transaction_key,
)

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


_suppress_broadcast = False

async def broadcast(msg_type, msg):
    if _suppress_broadcast:
        _LOGGER.debug("Broadcast suppressed: %s %s", msg_type, msg)
        return
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
# forward. Both are `None` while there is no live registration.
registration_expiry: float | None = None
registered_since: float | None = None
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
}

pending_transactions: dict[str, asyncio.Queue] = {}
incoming_requests: asyncio.Queue = None


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
            return sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
    _LOGGER.warning("Could not determine the local IP address")
    return "0.0.0.0"


def _gen(prefix="z9hG4bK"):
    return f"{prefix}{random.randint(100000, 9999999):x}"


def _next_cseq():
    global cseq_counter
    cseq_counter += 1
    return cseq_counter


# ─── Digest Auth ────────────────────────────────────────────────────

def _compute_ha1(realm):
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
    p = header_params(challenge)
    nonce = p.get("nonce", "")
    realm = p.get("realm", CFG.sip_domain)
    opaque = p.get("opaque", "")
    qop = p.get("qop", "")
    nc = "00000001"
    cnonce = f"{random.randint(10**7, 10**8-1):08x}"
    if "auth" in qop:
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


# ─── Transport ──────────────────────────────────────────────────────

def _create_ssl_context():
    ctx = ssl.create_default_context()
    if os.path.exists(C.CA_PATH):
        ctx.load_verify_locations(C.CA_PATH)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def connect():
    global reader, writer, lock
    loop = asyncio.get_event_loop()
    ctx = await loop.run_in_executor(None, _create_ssl_context)
    _LOGGER.info("Connecting to SIP proxy %s:%d...", CFG.proxy_host, CFG.proxy_port)
    reader, writer = await asyncio.open_connection(
        CFG.proxy_host, CFG.proxy_port, ssl=ctx, server_hostname=CFG.sni)
    lock = asyncio.Lock()
    _LOGGER.info("SIP TLS connected")


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
            try:
                await connect()
                if not await do_register():
                    raise ConnectionError("registration was refused")
                connected_at = time.monotonic()
                try:
                    await do_connect_profiles()
                except Exception as err:  # noqa: BLE001 - optional, never fatal
                    # A plant that rejects connectProfiles is still usable:
                    # the registration is what matters. Failing here would
                    # spin the supervisor forever on a healthy connection.
                    _LOGGER.warning("connectProfiles failed (%s); continuing", err)
                await _reader_loop()
                raise ConnectionError("connection closed by the server")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure means retry
                _clear_registration()
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
        buf = await _dispatch_buffer(buf)


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

_local_crypto_key = None
_local_video_crypto_key = None


def build_sdp():
    global _local_crypto_key, _local_video_crypto_key
    sid = str(int(time.time()))
    import base64 as _b64
    _local_crypto_key = _b64.b64encode(os.urandom(30)).decode()
    _local_video_crypto_key = _b64.b64encode(os.urandom(30)).decode()
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
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_crypto_key}\r\n"
        f"m=video {CFG.rtp_video_port} RTP/SAVP 96\r\n"
        f"b=AS:256\r\n"
        f"a=rtpmap:96 H264/90000\r\n"
        f"a=fmtp:96 profile-level-id=42801F;packetization-mode=1\r\n"
        f"a=rtcp-fb:96 ccm fir\r\n"
        f"a=rtcp-fb:96 nack\r\n"
        f"a=rtcp-fb:96 nack pli\r\n"
        f"a=sendrecv\r\n"
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_video_crypto_key}\r\n"
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
    global registration_expiry, registered_since
    granted = granted_expiry(msg, CFG.sip_user, C.DEFAULT_REGISTER_EXPIRY)
    if granted <= 0:
        _LOGGER.warning(
            "Registrar granted a zero lifetime; treating as not registered")
        _clear_registration()
        return False
    granted = max(granted, C.MIN_REGISTER_EXPIRY)
    now = time.monotonic()
    registration_expiry = now + granted
    registered_since = now
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
    global registration_expiry, registered_since
    _cancel_reregister()
    registration_expiry = None
    registered_since = None
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
    if not writer or writer.is_closing():
        await connect()

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


async def do_system_message(target_uri, body_text, extra_headers=None):
    if not registered:
        _LOGGER.warning("do_system_message: not registered, target=%s body=%s", target_uri, body_text)
        return False, "Non registrato"
    _LOGGER.info("do_system_message: target=%s body=%s headers=%s", target_uri, body_text, extra_headers)
    ftag = _gen("")
    cid = _gen("sys-")

    def _msg(branch, seq, auth=None):
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
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Content-Type: text/plain\r\n"
              f"Content-Length: {len(body_text)}\r\n\r\n{body_text}")
        return m

    branch = _gen()
    seq = _next_cseq()
    key = _open_transaction(branch, seq, "MESSAGE", cid)
    await send(_msg(branch, seq))
    for raw in await _wait_final(key, cid, seq, "MESSAGE", timeout=15):
        msg = parse_message(raw)
        _LOGGER.info("do_system_message: response %s for %s", msg.code, target_uri)
        if msg.code and msg.code < 200:
            continue
        if msg.code in (401, 407):
            ch = msg.headers.get("proxy-authenticate", "") or msg.headers.get("www-authenticate", "")
            if not ch:
                return False, f"Auth vuoto ({msg.code})"
            auth = _make_auth("MESSAGE", target_uri, ch)
            branch2 = _gen()
            seq2 = _next_cseq()
            key2 = _open_transaction(branch2, seq2, "MESSAGE", cid)
            await send(_msg(branch2, seq2, auth=auth))
            for raw2 in await _wait_final(key2, cid, seq2, "MESSAGE", timeout=15):
                msg2 = parse_message(raw2)
                _LOGGER.info("do_system_message: auth response %s for %s", msg2.code, target_uri)
                if msg2.code and 200 <= msg2.code < 300:
                    return True, f"OK ({msg2.code})"
                if msg2.code and msg2.code >= 300:
                    return False, f"Errore: {msg2.code}"
            return False, "Timeout"
        if msg.code and 200 <= msg.code < 300:
            return True, f"OK ({msg.code})"
        if msg.code and msg.code >= 300:
            return False, f"Errore: {msg.code}"
    return False, "Timeout"


async def do_call(target=None):
    """INVITE a SIP target (default: intercom targa 55001)."""
    if not registered:
        _LOGGER.error("do_call: NOT registered")
        return False, "Non registrato"
    if in_call or calling:
        _LOGGER.error("do_call: already in call/calling")
        return False, "Già in chiamata"

    _set_calling(True)
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

        vimar_callid = ''.join(random.choices(string.ascii_letters + string.digits, k=10))

        def _inv(branch, seq, auth=None):
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
                m += f"Proxy-Authorization: {auth}\r\n"
            m += (f"Mobile-IMEI: {CFG.device_id}\r\n"
                  f"MyName: {C.MY_NAME}\r\n"
                  f"X-Call-ID: {vimar_callid}\r\n"
                  f"Content-Type: application/sdp\r\n"
                  f"Content-Length: {len(sdp)}\r\n\r\n{sdp}")
            return m

        def _ack(to_tag, seq):
            branch = _gen()
            to_hdr = f"<{target_uri}>"
            if to_tag:
                to_hdr += f";tag={to_tag}"
            return (f"ACK {target_uri} SIP/2.0\r\n"
                    f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
                    f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
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
        await broadcast("log", "INVITE inviato...")

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
                ch = msg.headers.get("proxy-authenticate", "") or msg.headers.get("www-authenticate", "")
                if not ch:
                    return False, f"Auth vuoto ({msg.code})"
                auth = _make_auth("INVITE", target_uri, ch)
                _close_transaction(key, cid, cur_seq, "INVITE")
                branch = _gen()
                cur_seq = _next_cseq()
                key = _open_transaction(branch, cur_seq, "INVITE", cid)
                queue = pending_transactions[key]
                await send(_inv(branch, cur_seq, auth=auth))
                continue

            if 200 <= msg.code < 300:
                call_state["to_tag"] = ttag
                raw_contact = msg.headers.get("contact", "")
                if "<" in raw_contact and ">" in raw_contact:
                    call_state["remote_contact"] = raw_contact[raw_contact.index("<")+1:raw_contact.index(">")]
                else:
                    call_state["remote_contact"] = raw_contact
                await send(_ack(ttag, cur_seq))

                if msg.body:
                    remote = parse_sdp(msg.body)
                    call_state["remote_sdp"] = remote
                    _LOGGER.info("SDP: audio=%s video=%s", remote.get('audio', {}), remote.get('video', {}))
                    await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)

                _set_in_call(True)
                _set_calling(False)
                await broadcast("call_started", "Connesso!")
                # Request keyframe immediately — no delay
                await send_keyframe_request()
                return True, "Connesso!"

            if msg.code >= 300:
                _LOGGER.error("INVITE rejected: %d", msg.code)
                await send(_ack(ttag, cur_seq))
                reason = (msg.start_line.split(" ", 2)[2]
                          if msg.start_line.count(" ") >= 2 else str(msg.code))
                return False, f"{msg.code} {reason}"

        _LOGGER.error("INVITE timeout (45s) for %s", target_uri)
        return False, "Timeout (45s)"
    finally:
        if key is not None:
            _close_transaction(key, cid, cur_seq, "INVITE")
        if not in_call:
            _set_calling(False)


async def send_keyframe_request():
    """Send SIP INFO picture_fast_update to get a video keyframe (SPS/PPS)."""
    if not in_call or not call_state["call_id"]:
        return
    info_target = call_state.get("remote_contact") or CFG.panel_uri(CFG.default_panel.address)
    to_uri = call_state.get("original_target") or CFG.panel_uri(CFG.default_panel.address)
    seq = _next_cseq()
    body = ('<?xml version="1.0" encoding="utf-8" ?>'
            '<media_control><vc_primitive><to_encoder>'
            '<picture_fast_update></picture_fast_update>'
            '</to_encoder></vc_primitive></media_control>')
    msg = (
        f"INFO {info_target} SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={_gen()};rport\r\n"
        f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
        f"Max-Forwards: 70\r\n"
        f"To: <{to_uri}>;tag={call_state['to_tag']}\r\n"
        f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={call_state['from_tag']}\r\n"
        f"Call-ID: {call_state['call_id']}\r\n"
        f"CSeq: {seq} INFO\r\n"
        f"Content-Type: application/media_control+xml\r\n"
        f"Content-Length: {len(body)}\r\n\r\n{body}")
    await send(msg)
    _LOGGER.info("Sent INFO picture_fast_update (keyframe request)")


async def do_hangup():
    _set_calling(False)
    if not in_call or not call_state["call_id"]:
        _set_in_call(False)
        await media.stop_media()
        await broadcast("call_ended", "Chiamata terminata")
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
           f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
           f"Max-Forwards: 70\r\n"
           f"To: {to_hdr}\r\n"
           f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: {seq} BYE\r\n"
           f"User-Agent: {CFG.user_agent}\r\n"
           f"Content-Length: 0\r\n\r\n")
    key = _open_transaction(branch, seq, "BYE", cid)
    await send(bye)
    await _wait_final(key, cid, seq, "BYE", timeout=5)

    _set_in_call(False)
    call_state.update(call_id=None, from_tag=None, to_tag=None,
                      remote_contact=None, remote_sdp=None, original_target=None)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")


async def do_door(target: str | None = None):
    """Legacy door open — prefer do_system_message via hub.async_door."""
    address = target or CFG.default_panel.address
    uri = CFG.panel_uri(address)
    _LOGGER.info("do_door: sending %s to %s", CFG.door_command, uri)
    return await do_system_message(
        uri, CFG.door_command, extra_headers={"Panda": "command"})


async def do_options(target=None):
    if not registered:
        return False, "Non registrato"
    target = target or CFG.panel_uri(CFG.default_panel.address)
    ftag = _gen("")
    cid = _gen("opt-")

    def _msg(branch, seq, auth=None):
        m = (f"OPTIONS {target} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:{C.SIP_LOCAL_PORT};branch={branch};rport\r\n"
             f"Route: <sip:{CFG.route};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <{target}>\r\n"
             f"From: <sip:{CFG.sip_user}@{CFG.sip_domain}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} OPTIONS\r\n"
             f"User-Agent: {CFG.user_agent}\r\n"
             f"Accept: application/sdp\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    branch = _gen()
    seq = _next_cseq()
    key = _open_transaction(branch, seq, "OPTIONS", cid)
    await send(_msg(branch, seq))
    for raw in await _wait_final(key, cid, seq, "OPTIONS"):
        msg = parse_message(raw)
        if msg.code and msg.code < 200:
            continue
        if msg.code in (401, 407):
            ch = msg.headers.get("proxy-authenticate", "") or msg.headers.get("www-authenticate", "")
            if not ch:
                return False, "Auth vuoto"
            auth = _make_auth("OPTIONS", target, ch)
            branch2 = _gen()
            seq2 = _next_cseq()
            key2 = _open_transaction(branch2, seq2, "OPTIONS", cid)
            await send(_msg(branch2, seq2, auth=auth))
            for raw2 in await _wait_final(key2, cid, seq2, "OPTIONS"):
                msg2 = parse_message(raw2)
                if msg2.code and 200 <= msg2.code < 300:
                    return True, f"OK: {msg2.code}"
                return False, f"Errore: {msg2.code}"
            return False, "Timeout"
        if msg.code and 200 <= msg.code < 300:
            return True, f"OK: {msg.code}"
        if msg.code and msg.code >= 300:
            return False, f"Errore: {msg.code}"
    return False, "Timeout"


async def do_connect_profiles():
    """Register push profile on Vimar cloud. Uses Digest auth (not Basic)."""
    username = f"{CFG.sip_user}@{CFG.sip_domain}"
    body = [{"sipid": CFG.sip_user, "domain": CFG.sip_domain, "pntok": CFG.push_token}]
    if not CFG.push_token:
        return False, "No FCM token"

    import requests as req_lib
    loop = asyncio.get_event_loop()

    def _call(endpoint):
        return req_lib.post(
            f"https://ipvdes.vimar.cloud/eipvdesUtils/{endpoint}",
            json=body,
            auth=req_lib.auth.HTTPDigestAuth(username, CFG.push_token),
            headers={"Accept": "application/json"}, timeout=15)

    try:
        resp = await loop.run_in_executor(None, _call, "connectProfiles")
        _LOGGER.info("connectProfiles: %d", resp.status_code)
        if resp.status_code == 200:
            return True, "Profilo connesso"
        if resp.status_code == 403:
            await loop.run_in_executor(None, _call, "disconnectProfiles")
            resp3 = await loop.run_in_executor(None, _call, "connectProfiles")
            _LOGGER.info("connectProfiles retry: %d", resp3.status_code)
            if resp3.status_code == 200:
                return True, "Profilo connesso"
            return False, f"connectProfiles: {resp3.status_code}"
        return False, f"connectProfiles: {resp.status_code}"
    except Exception as e:
        _LOGGER.error("connectProfiles error: %s", e)
        return False, str(e)


# ─── Incoming SIP ───────────────────────────────────────────────────

pending_incoming = {
    "active": False, "cid": None, "from_hdr": None, "to_hdr": None,
    "cseq": None, "via_block": None, "my_tag": None,
    "caller_uri": None, "caller_tag": None, "body": None,
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

    pending_incoming.update(
        active=True, cid=cid, from_hdr=from_hdr, to_hdr=to_hdr,
        cseq=cseq, via_block=via_block, my_tag=my_tag,
        caller_uri=caller_uri, caller_tag=caller_tag, body=msg.body,
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
        f"Contact: <sip:{CFG.sip_user}@{MY_IP}:{C.SIP_LOCAL_PORT};transport=tls>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n{sdp}")

    _set_in_call(True)
    call_state["call_id"] = p["cid"]
    call_state["from_tag"] = p["my_tag"]
    call_state["to_tag"] = p["caller_tag"]
    call_state["remote_contact"] = p["caller_uri"]

    if p["body"]:
        remote = parse_sdp(p["body"])
        call_state["remote_sdp"] = remote
        _LOGGER.info("Answer SDP: audio=%s video=%s", remote.get('audio'), remote.get('video'))
        await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)

    pending_incoming["active"] = False
    await broadcast("call_started", "Chiamata attiva!")
    # Request keyframe for video
    await send_keyframe_request()
    return True, "Answered"


async def do_decline_incoming():
    if not pending_incoming["active"]:
        return

    p = pending_incoming
    await send(
        f"SIP/2.0 603 Decline\r\n"
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

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    _set_in_call(False)
    call_state.update(call_id=None, from_tag=None, to_tag=None,
                      remote_contact=None, remote_sdp=None, original_target=None)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")


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

    if pending_incoming["active"] and pending_incoming["cid"] == cid:
        p = pending_incoming
        invite_cseq = p["cseq"]
        await send(
            f"SIP/2.0 487 Request Terminated\r\n"
            f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {invite_cseq}\r\n"
            f"Content-Length: 0\r\n\r\n")
        pending_incoming["active"] = False

    await broadcast("ring_ended", "Chiamata cancellata")


async def request_processor():
    while True:
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
            _LOGGER.info("SIP MESSAGE: %s", msg.body[:200])
            await broadcast("message", msg.body[:200])
            from_hdr = msg.headers.get("from", "")
            to_hdr = msg.headers.get("to", "")
            msg_cid = msg.headers.get("call-id", "")
            msg_cseq = msg.headers.get("cseq", "1 MESSAGE")
            await send(
                f"SIP/2.0 200 OK\r\n"
                f"{_via_block(msg)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                f"Call-ID: {msg_cid}\r\nCSeq: {msg_cseq}\r\n"
                f"Content-Length: 0\r\n\r\n")
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
