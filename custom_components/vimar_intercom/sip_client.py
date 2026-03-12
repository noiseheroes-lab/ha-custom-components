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

_LOGGER = logging.getLogger(__name__)

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

# State change callback — hub sets this to notify entities
_state_change_callback = None

call_state = {
    "call_id": None, "from_tag": None, "to_tag": None,
    "remote_contact": None, "remote_sdp": None, "original_target": None,
}

pending_responses: dict[str, asyncio.Queue] = {}
incoming_requests: asyncio.Queue = None


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


def get_local_ip():
    """Detect local IP by connecting to the SIP proxy (cloud)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Use cloud proxy for detection — always reachable
        s.connect((C.SIP_PROXY, C.SIP_PORT))
        ip = s.getsockname()[0]
        _LOGGER.info("Detected local IP: %s", ip)
        return ip
    except Exception:
        # Fallback: try local Tab5S
        try:
            s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s2.connect((C.LOCAL_PROXY, C.LOCAL_SIP_PORT))
            ip = s2.getsockname()[0]
            s2.close()
            _LOGGER.info("Detected local IP (via Tab5S): %s", ip)
            return ip
        except Exception:
            _LOGGER.warning("IP detection failed, using fallback")
            return "0.0.0.0"
    finally:
        s.close()


def _gen(prefix="z9hG4bK"):
    return f"{prefix}{random.randint(100000, 9999999):x}"


def _next_cseq():
    global cseq_counter
    cseq_counter += 1
    return cseq_counter


# ─── Digest Auth ────────────────────────────────────────────────────

def _compute_ha1(realm):
    if realm == C.SIP_DOMAIN:
        return C.SIP_HA1
    return hashlib.md5(f"{C.SIP_USER}:{realm}:{C.SIP_PASSWORD}".encode()).hexdigest()


def _digest_resp(method, uri, nonce, realm=None, qop=None, nc=None, cnonce=None):
    ha1 = _compute_ha1(realm or C.SIP_DOMAIN)
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if qop == "auth":
        return hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


def _make_auth(method, uri, challenge):
    p = {}
    for item in challenge.replace("Digest ", "").split(","):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            p[k.strip()] = v.strip().strip('"')
    nonce = p.get("nonce", "")
    realm = p.get("realm", C.SIP_DOMAIN)
    opaque = p.get("opaque", "")
    qop = p.get("qop", "")
    nc = "00000001"
    cnonce = f"{random.randint(10**7, 10**8-1):08x}"
    if "auth" in qop:
        resp = _digest_resp(method, uri, nonce, realm, "auth", nc, cnonce)
        hdr = (f'Digest username="{C.SIP_USER}", realm="{realm}", '
               f'nonce="{nonce}", uri="{uri}", response="{resp}", '
               f'algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        resp = _digest_resp(method, uri, nonce, realm)
        hdr = (f'Digest username="{C.SIP_USER}", realm="{realm}", '
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
    _LOGGER.info("Connecting to SIP proxy %s:%d...", C.SIP_PROXY, C.SIP_PORT)
    reader, writer = await asyncio.open_connection(
        C.SIP_PROXY, C.SIP_PORT, ssl=ctx, server_hostname=C.SIP_SNI)
    lock = asyncio.Lock()
    _LOGGER.info("SIP TLS connected")


async def reconnect():
    """Reconnect TLS with exponential backoff."""
    global reader, writer
    _set_registered(False)
    delays = [2, 4, 8, 16, 32]
    for attempt, delay in enumerate(delays, 1):
        _LOGGER.warning("SIP reconnect attempt %d/%d in %ds...", attempt, len(delays), delay)
        await asyncio.sleep(delay)
        try:
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass
            await connect()
            _LOGGER.info("SIP reconnected, re-registering...")
            ok = await do_register()
            if ok:
                try:
                    await do_connect_profiles()
                except Exception:
                    pass
                return True
        except Exception as e:
            _LOGGER.error("Reconnect attempt %d failed: %s", attempt, e)
    _LOGGER.error("All reconnect attempts failed")
    return False


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


def _parse(msg):
    parts = msg.split("\r\n\r\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    lines = parts[0].split("\r\n")
    first = lines[0]
    code = method = None
    if first.startswith("SIP/2.0"):
        try:
            code = int(first.split()[1])
        except (ValueError, IndexError):
            pass
    else:
        method = first.split()[0] if first else None
    hdrs = {}
    via_list = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key == "via":
                via_list.append(v.strip())
            hdrs[key] = v.strip()
    if via_list:
        hdrs["_via_all"] = via_list
    return (code or method), hdrs, body, first


def _call_id(hdrs):
    return hdrs.get("call-id", "")


def _tag(header_val):
    for part in header_val.split(";"):
        part = part.strip()
        if part.startswith("tag="):
            return part[4:]
    return ""


# ─── Reader task ────────────────────────────────────────────────────

async def reader_task():
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=30)
            if not chunk:
                _LOGGER.warning("SIP connection closed by server, reconnecting...")
                await reconnect()
                buf = b""
                continue
            buf += chunk
        except asyncio.TimeoutError:
            # Send CRLF keepalive (RFC 5626) to prevent proxy from
            # considering TLS connection stale
            try:
                async with lock:
                    writer.write(b"\r\n\r\n")
                    await writer.drain()
            except Exception:
                _LOGGER.warning("CRLF keepalive failed, reconnecting...")
                await reconnect()
                buf = b""
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOGGER.error("SIP reader error: %s, reconnecting...", e)
            try:
                await reconnect()
            except Exception as re:
                _LOGGER.error("Reconnect failed: %s", re)
            buf = b""
            await asyncio.sleep(2)
            continue

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

            kind, hdrs, body, first = _parse(raw)
            cid = _call_id(hdrs)
            _LOGGER.debug("[SIP <<<] %s", first)

            if isinstance(kind, int):
                if cid in pending_responses:
                    _LOGGER.debug("reader: queuing response %d for cid=%s", kind, cid[:24])
                    await pending_responses[cid].put(raw)
                else:
                    _LOGGER.warning("Stale response %d for cid=%s (known: %s)", kind, cid[:24],
                                    list(pending_responses.keys())[:3])
            elif isinstance(kind, str):
                await incoming_requests.put(raw)


async def _wait_final(cid, timeout=15):
    q = pending_responses.setdefault(cid, asyncio.Queue())
    results = []
    deadline = time.time() + timeout
    while True:
        rem = deadline - time.time()
        if rem <= 0:
            break
        try:
            raw = await asyncio.wait_for(q.get(), timeout=min(rem, 3))
            results.append(raw)
            kind, *_ = _parse(raw)
            if isinstance(kind, int) and kind >= 200:
                break
        except asyncio.TimeoutError:
            continue
    pending_responses.pop(cid, None)
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
        f"m=audio {C.RTP_AUDIO_PORT} RTP/SAVP 0 8 101\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
        f"a=rtpmap:8 PCMA/8000\r\n"
        f"a=rtpmap:101 telephone-event/8000\r\n"
        f"a=fmtp:101 0-15\r\n"
        f"a=ptime:20\r\n"
        f"a=sendrecv\r\n"
        f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{_local_crypto_key}\r\n"
        f"m=video {C.RTP_VIDEO_PORT} RTP/SAVP 96\r\n"
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

async def do_register():
    if not writer or writer.is_closing():
        await connect()

    global local_tag
    local_tag = _gen("")
    cid = _gen("reg-")
    uri = f"sip:{C.SIP_DOMAIN}"

    def _msg(auth=None, seq=1):
        branch = _gen()
        contact_uri = f"sip:{C.SIP_USER}@{MY_IP}:5070;transport=tls"
        if C.PN_TOKEN:
            contact_uri += (f";app-id={C.PN_APP_ID}"
                           f";pn-type={C.PN_TYPE}"
                           f";pn-tok={C.PN_TOKEN}"
                           f";pn-msg-str=IM_MSG;pn-msg-snd=msg.caf"
                           f";pn-call-str=IC_MSG;pn-call-snd=notes_of_the_optimistic.caf"
                           f";q=0.00;domain-name={C.SIP_DOMAIN}")
        contact = f"<{contact_uri}>"
        contact += f';+sip.instance="<urn:uuid:{C.DEVICE_UUID}>"'
        contact += f";expires={'5184000' if C.PN_TOKEN else '3600'}"
        m = (f"REGISTER {uri} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={branch};rport\r\n"
             f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>\r\n"
             f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={local_tag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} REGISTER\r\n"
             f"Contact: {contact}\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Mobile-IMEI: {C.DEVICE_IMEI}\r\n"
             f"MyName: {C.MY_NAME}\r\n"
             f"Supported: replaces,outbound,gruu\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n")
        if auth:
            m += f"Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    s1 = _next_cseq()
    await send(_msg(seq=s1))
    resps = await _wait_final(cid)

    for r in resps:
        code, hdrs, *_ = _parse(r)
        if code == 401:
            ch = hdrs.get("www-authenticate", "")
            if not ch:
                return False
            auth = _make_auth("REGISTER", uri, ch)
            await send(_msg(auth=auth, seq=_next_cseq()))
            for r2 in await _wait_final(cid):
                if _parse(r2)[0] == 200:
                    _set_registered(True)
                    _LOGGER.info("SIP registered successfully")
                    return True
            return False
        elif code == 200:
            _set_registered(True)
            _LOGGER.info("SIP registered successfully")
            return True
    return False


async def do_system_message(target_uri, body_text, extra_headers=None):
    if not registered:
        _LOGGER.warning("do_system_message: not registered, target=%s body=%s", target_uri, body_text)
        return False, "Non registrato"
    _LOGGER.info("do_system_message: target=%s body=%s headers=%s", target_uri, body_text, extra_headers)
    ftag = _gen("")
    cid = _gen("sys-")

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"MESSAGE {target_uri} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={branch};rport\r\n"
             f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} MESSAGE\r\n"
             f"Contact: <sip:{C.SIP_USER}@{MY_IP}:5070;transport=tls>\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Mobile-IMEI: {C.DEVICE_IMEI}\r\n"
             f"MyName: {C.MY_NAME}\r\n")
        if extra_headers:
            for k, v in extra_headers.items():
                m += f"{k}: {v}\r\n"
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Content-Type: text/plain\r\n"
              f"Content-Length: {len(body_text)}\r\n\r\n{body_text}")
        return m

    await send(_msg(seq=_next_cseq()))
    for r in await _wait_final(cid, timeout=15):
        code, hdrs, *_ = _parse(r)
        _LOGGER.info("do_system_message: response %s for %s", code, target_uri)
        if code and code < 200:
            continue
        if code in (401, 407):
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                return False, f"Auth vuoto ({code})"
            auth = _make_auth("MESSAGE", target_uri, ch)
            await send(_msg(auth=auth, seq=_next_cseq()))
            for r2 in await _wait_final(cid, timeout=15):
                c2 = _parse(r2)[0]
                _LOGGER.info("do_system_message: auth response %s for %s", c2, target_uri)
                if c2 and 200 <= c2 < 300:
                    return True, f"OK ({c2})"
                if c2 and c2 >= 300:
                    return False, f"Errore: {c2}"
            return False, "Timeout"
        if code and 200 <= code < 300:
            return True, f"OK ({code})"
        if code and code >= 300:
            return False, f"Errore: {code}"
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
    target_uri = target or C.INTERCOM
    _LOGGER.info("do_call: target=%s", target_uri)
    ftag = _gen("")
    cid = _gen("call-")
    sdp = build_sdp()
    call_state["call_id"] = cid
    call_state["from_tag"] = ftag
    call_state["original_target"] = target_uri

    vimar_callid = ''.join(random.choices(string.ascii_letters + string.digits, k=10))

    def _inv(auth=None, seq=1):
        branch = _gen()
        m = (f"INVITE {target_uri} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={branch};rport\r\n"
             f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <{target_uri}>\r\n"
             f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} INVITE\r\n"
             f"Contact: <sip:{C.SIP_USER}@{MY_IP}:5070;transport=tls>"
             f';+sip.instance="<urn:uuid:{C.DEVICE_UUID}>"\r\n'
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Supported: replaces,outbound,gruu,timer\r\n"
             f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
             f"Session-Expires: 600;refresher=uas\r\n"
             f"Min-SE: 90\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        m += (f"Mobile-IMEI: {C.DEVICE_IMEI}\r\n"
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
                f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={branch};rport\r\n"
                f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
                f"Max-Forwards: 70\r\n"
                f"To: {to_hdr}\r\n"
                f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={ftag}\r\n"
                f"Call-ID: {cid}\r\n"
                f"CSeq: {seq} ACK\r\n"
                f"Content-Length: 0\r\n\r\n")

    cur_seq = _next_cseq()
    await send(_inv(seq=cur_seq))
    await broadcast("log", "INVITE inviato...")

    q = pending_responses.setdefault(cid, asyncio.Queue())
    _LOGGER.debug("do_call: cid=%s, q id=%s, pending_keys=%s", cid[:24], id(q), list(pending_responses.keys())[:3])
    deadline = time.time() + 45

    while time.time() < deadline:
        _LOGGER.debug("do_call: waiting q.get (qsize=%d, cid_in_pending=%s, q_is_same=%s)",
                       q.qsize(), cid in pending_responses, pending_responses.get(cid) is q)
        try:
            raw = await asyncio.wait_for(q.get(), timeout=3)
        except asyncio.TimeoutError:
            _LOGGER.debug("do_call: q.get timeout (qsize=%d)", q.qsize())
            continue

        code, hdrs, body, first = _parse(raw)
        ttag = _tag(hdrs.get("to", ""))
        _LOGGER.debug("do_call: response %s (body=%dB)", code, len(body) if body else 0)

        if code in (100, 180, 183):
            if code == 183 and body:
                call_state["remote_sdp"] = parse_sdp(body)
            continue

        if code in (401, 407):
            await send(_ack(ttag, cur_seq))
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                pending_responses.pop(cid, None)
                _set_calling(False)
                return False, f"Auth vuoto ({code})"
            auth = _make_auth("INVITE", target_uri, ch)
            cur_seq = _next_cseq()
            await send(_inv(auth=auth, seq=cur_seq))
            continue

        if 200 <= code < 300:
            call_state["to_tag"] = ttag
            raw_contact = hdrs.get("contact", "")
            if "<" in raw_contact and ">" in raw_contact:
                call_state["remote_contact"] = raw_contact[raw_contact.index("<")+1:raw_contact.index(">")]
            else:
                call_state["remote_contact"] = raw_contact
            await send(_ack(ttag, cur_seq))

            if body:
                remote = parse_sdp(body)
                call_state["remote_sdp"] = remote
                _LOGGER.info("SDP: audio=%s video=%s", remote.get('audio', {}), remote.get('video', {}))
                await media.setup_media(remote, _local_crypto_key, _local_video_crypto_key)

            _set_in_call(True)
            _set_calling(False)
            await broadcast("call_started", "Connesso!")
            # Request keyframe immediately — no delay
            await send_keyframe_request()
            pending_responses.pop(cid, None)
            return True, "Connesso!"

        if code >= 300:
            _LOGGER.error("INVITE rejected: %d", code)
            await send(_ack(ttag, cur_seq))
            pending_responses.pop(cid, None)
            _set_calling(False)
            reason = first.split(" ", 2)[2] if first.count(" ") >= 2 else str(code)
            return False, f"{code} {reason}"

    pending_responses.pop(cid, None)
    _set_calling(False)
    _LOGGER.error("INVITE timeout (45s) for %s", target_uri)
    return False, "Timeout (45s)"


async def send_keyframe_request():
    """Send SIP INFO picture_fast_update to get a video keyframe (SPS/PPS)."""
    if not in_call or not call_state["call_id"]:
        return
    info_target = call_state.get("remote_contact") or C.INTERCOM
    to_uri = call_state.get("original_target") or C.INTERCOM
    seq = _next_cseq()
    body = ('<?xml version="1.0" encoding="utf-8" ?>'
            '<media_control><vc_primitive><to_encoder>'
            '<picture_fast_update></picture_fast_update>'
            '</to_encoder></vc_primitive></media_control>')
    msg = (
        f"INFO {info_target} SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={_gen()};rport\r\n"
        f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
        f"Max-Forwards: 70\r\n"
        f"To: <{to_uri}>;tag={call_state['to_tag']}\r\n"
        f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={call_state['from_tag']}\r\n"
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

    target_uri = call_state.get("remote_contact") or C.INTERCOM
    to_uri = call_state.get("original_target") or C.INTERCOM
    to_hdr = f"<{to_uri}>"
    if ttag:
        to_hdr += f";tag={ttag}"

    bye = (f"BYE {target_uri} SIP/2.0\r\n"
           f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={_gen()};rport\r\n"
           f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
           f"Max-Forwards: 70\r\n"
           f"To: {to_hdr}\r\n"
           f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={ftag}\r\n"
           f"Call-ID: {cid}\r\n"
           f"CSeq: {_next_cseq()} BYE\r\n"
           f"User-Agent: {C.USER_AGENT}\r\n"
           f"Content-Length: 0\r\n\r\n")
    await send(bye)
    await _wait_final(cid, timeout=5)

    _set_in_call(False)
    call_state.update(call_id=None, from_tag=None, to_tag=None,
                      remote_contact=None, remote_sdp=None, original_target=None)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")


async def do_door():
    """Legacy door open — prefer do_system_message via hub.async_door."""
    _LOGGER.info("do_door: sending %s to %s", C.DOOR_COMMAND, C.DOOR_ESTERNO)
    return await do_system_message(
        C.DOOR_ESTERNO, C.DOOR_COMMAND, extra_headers={"Panda": "command"})


async def do_options(target=None):
    if not registered:
        return False, "Non registrato"
    target = target or C.INTERCOM
    ftag = _gen("")
    cid = _gen("opt-")

    def _msg(auth=None, seq=1):
        branch = _gen()
        m = (f"OPTIONS {target} SIP/2.0\r\n"
             f"Via: SIP/2.0/TLS {MY_IP}:5070;branch={branch};rport\r\n"
             f"Route: <sip:{C.SIP_ROUTE};transport=tls;lr>\r\n"
             f"Max-Forwards: 70\r\n"
             f"To: <{target}>\r\n"
             f"From: <sip:{C.SIP_USER}@{C.SIP_DOMAIN}>;tag={ftag}\r\n"
             f"Call-ID: {cid}\r\n"
             f"CSeq: {seq} OPTIONS\r\n"
             f"User-Agent: {C.USER_AGENT}\r\n"
             f"Accept: application/sdp\r\n")
        if auth:
            m += f"Proxy-Authorization: {auth}\r\n"
        return m + "Content-Length: 0\r\n\r\n"

    await send(_msg(seq=_next_cseq()))
    for r in await _wait_final(cid):
        code, hdrs, *_ = _parse(r)
        if code and code < 200:
            continue
        if code in (401, 407):
            ch = hdrs.get("proxy-authenticate", "") or hdrs.get("www-authenticate", "")
            if not ch:
                return False, "Auth vuoto"
            await send(_msg(auth=_make_auth("OPTIONS", target, ch), seq=_next_cseq()))
            for r2 in await _wait_final(cid):
                c2 = _parse(r2)[0]
                if c2 and 200 <= c2 < 300:
                    return True, f"OK: {c2}"
                return False, f"Errore: {c2}"
            return False, "Timeout"
        if code and 200 <= code < 300:
            return True, f"OK: {code}"
        if code and code >= 300:
            return False, f"Errore: {code}"
    return False, "Timeout"


async def do_connect_profiles():
    """Register push profile on Vimar cloud. Uses Digest auth (not Basic)."""
    username = f"{C.SIP_USER}@{C.SIP_DOMAIN}"
    body = [{"sipid": C.SIP_USER, "domain": C.SIP_DOMAIN, "pntok": C.PN_TOKEN}]
    if not C.PN_TOKEN:
        return False, "No FCM token"

    import requests as req_lib
    loop = asyncio.get_event_loop()

    def _call(endpoint):
        return req_lib.post(
            f"https://ipvdes.vimar.cloud/eipvdesUtils/{endpoint}",
            json=body,
            auth=req_lib.auth.HTTPDigestAuth(username, C.PN_TOKEN),
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
    _, hdrs, body, first = _parse(raw)
    from_hdr = hdrs.get("from", "?")
    cid = _call_id(hdrs)
    via_block = _via_block(hdrs)
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 INVITE")

    caller_tag = _tag(from_hdr)
    caller_uri = ""
    if "<" in from_hdr and ">" in from_hdr:
        caller_uri = from_hdr[from_hdr.index("<")+1:from_hdr.index(">")]

    _LOGGER.info("Incoming INVITE from %s", caller_uri)

    my_tag = _gen("")

    pending_incoming.update(
        active=True, cid=cid, from_hdr=from_hdr, to_hdr=to_hdr,
        cseq=cseq, via_block=via_block, my_tag=my_tag,
        caller_uri=caller_uri, caller_tag=caller_tag, body=body,
    )

    await send(
        f"SIP/2.0 180 Ringing\r\n"
        f"{via_block}To: {to_hdr};tag={my_tag}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Contact: <sip:{C.SIP_USER}@{MY_IP}:5070;transport=tls>\r\n"
        f"Content-Length: 0\r\n\r\n")

    await broadcast("ring", f"Chiamata da: {caller_uri}")


async def do_answer_incoming():
    if not pending_incoming["active"]:
        return False, "Nessuna chiamata in arrivo"

    p = pending_incoming
    sdp = build_sdp()

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{p['via_block']}To: {p['to_hdr']};tag={p['my_tag']}\r\nFrom: {p['from_hdr']}\r\n"
        f"Call-ID: {p['cid']}\r\nCSeq: {p['cseq']}\r\n"
        f"Contact: <sip:{C.SIP_USER}@{MY_IP}:5070;transport=tls>\r\n"
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
    return True, "Risposto!"


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


def _via_block(hdrs):
    via_all = hdrs.get("_via_all", [])
    if via_all:
        return "".join(f"Via: {v}\r\n" for v in via_all)
    return f"Via: {hdrs.get('via', '')}\r\n"


async def handle_incoming_bye(raw):
    _, hdrs, *_ = _parse(raw)
    cid = _call_id(hdrs)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 BYE")

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Content-Length: 0\r\n\r\n")

    _set_in_call(False)
    call_state.update(call_id=None, from_tag=None, to_tag=None,
                      remote_contact=None, remote_sdp=None, original_target=None)
    await media.stop_media()
    await broadcast("call_ended", "Chiamata terminata")


async def handle_incoming_options(raw):
    _, hdrs, *_ = _parse(raw)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cid = _call_id(hdrs)
    cseq = hdrs.get("cseq", "1 OPTIONS")
    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
        f"Call-ID: {cid}\r\nCSeq: {cseq}\r\n"
        f"Allow: INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,INFO,MESSAGE,UPDATE\r\n"
        f"Content-Length: 0\r\n\r\n")


async def handle_incoming_cancel(raw):
    _, hdrs, *_ = _parse(raw)
    cid = _call_id(hdrs)
    from_hdr = hdrs.get("from", "")
    to_hdr = hdrs.get("to", "")
    cseq = hdrs.get("cseq", "1 CANCEL")

    _LOGGER.info("Incoming CANCEL for %s", cid[:24])

    await send(
        f"SIP/2.0 200 OK\r\n"
        f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
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
        kind, hdrs, body, _ = _parse(raw)
        if kind == "INVITE":
            asyncio.create_task(handle_incoming_invite(raw))
        elif kind == "CANCEL":
            asyncio.create_task(handle_incoming_cancel(raw))
        elif kind == "BYE":
            asyncio.create_task(handle_incoming_bye(raw))
        elif kind == "OPTIONS":
            asyncio.create_task(handle_incoming_options(raw))
        elif kind == "MESSAGE":
            _LOGGER.info("SIP MESSAGE: %s", body[:200])
            await broadcast("message", body[:200])
            from_hdr = hdrs.get("from", "")
            to_hdr = hdrs.get("to", "")
            msg_cid = hdrs.get("call-id", "")
            msg_cseq = hdrs.get("cseq", "1 MESSAGE")
            await send(
                f"SIP/2.0 200 OK\r\n"
                f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                f"Call-ID: {msg_cid}\r\nCSeq: {msg_cseq}\r\n"
                f"Content-Length: 0\r\n\r\n")
        elif kind == "INFO":
            from_hdr = hdrs.get("from", "")
            to_hdr = hdrs.get("to", "")
            info_cid = hdrs.get("call-id", "")
            info_cseq = hdrs.get("cseq", "1 INFO")
            await send(
                f"SIP/2.0 200 OK\r\n"
                f"{_via_block(hdrs)}To: {to_hdr}\r\nFrom: {from_hdr}\r\n"
                f"Call-ID: {info_cid}\r\nCSeq: {info_cseq}\r\n"
                f"Content-Length: 0\r\n\r\n")
        elif kind != "ACK":
            _LOGGER.debug("Unhandled SIP request: %s", kind)
