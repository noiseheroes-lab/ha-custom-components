"""Vimar Intercom — Media: RTP transport, STUN, G.711 codec, video capture, audio."""

import asyncio
import logging
import os
import random
import socket
import struct
import subprocess

from .const import (
    RTP_AUDIO_PORT, RTP_VIDEO_PORT,
    FFMPEG_AV_VIDEO_PORT, FFMPEG_AV_AUDIO_PORT,
)
from .srtp import SRTPContext

_LOGGER = logging.getLogger(__name__)

# ─── Broadcast callback (set by main.py) ────────────────────────────
_broadcast = None


def init(broadcast_fn):
    global _broadcast
    _broadcast = broadcast_fn


async def broadcast(msg_type, msg):
    if _broadcast:
        await _broadcast(msg_type, msg)


# ─── G.711 μ-law codec ──────────────────────────────────────────────

def _build_ulaw_decode_table():
    table = []
    for byte_val in range(256):
        b = ~byte_val & 0xFF
        sign = b & 0x80
        exponent = (b >> 4) & 0x07
        mantissa = b & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        table.append(-sample if sign else sample)
    return table

_ULAW_DECODE = _build_ulaw_decode_table()


def ulaw_decode(data: bytes) -> bytes:
    """μ-law bytes → 16-bit signed LE PCM."""
    pcm = bytearray(len(data) * 2)
    for i, b in enumerate(data):
        struct.pack_into('<h', pcm, i * 2, _ULAW_DECODE[b])
    return bytes(pcm)


def ulaw_encode(pcm_data: bytes) -> bytes:
    """16-bit signed LE PCM → μ-law bytes."""
    BIAS = 0x84
    CLIP = 32635
    n = len(pcm_data) // 2
    out = bytearray(n)
    for i in range(n):
        sample = struct.unpack_from('<h', pcm_data, i * 2)[0]
        sign = 0x80 if sample < 0 else 0
        if sample < 0:
            sample = -sample
        sample = min(sample, CLIP) + BIAS
        exp = 7
        mask = 0x4000
        while exp > 0 and not (sample & mask):
            exp -= 1
            mask >>= 1
        mantissa = (sample >> (exp + 3)) & 0x0F
        out[i] = (~(sign | (exp << 4) | mantissa)) & 0xFF
    return bytes(out)


# ─── RTP Protocols ──────────────────────────────────────────────────

class RTPAudioProtocol(asyncio.DatagramProtocol):
    """Audio SRTP: receive SRTP PCMU → decrypt → decode → buffer. Send as SRTP.
    Also forwards decrypted RTP to a secondary port for AV ffmpeg."""

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        self.audio_buffer = asyncio.Queue(maxsize=200)
        self.rtp_seq = random.randint(0, 65535)
        self.rtp_ts = random.randint(0, 2**32 - 1)
        self.rtp_ssrc = random.randint(0, 2**32 - 1)
        self.pkt_count = 0
        self.srtp_rx: SRTPContext | None = None
        self.srtp_tx: SRTPContext | None = None
        # Forward raw RTP to AV ffmpeg
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.info("RTP Audio ready on :%d", RTP_AUDIO_PORT)

    def datagram_received(self, data, addr):
        if len(data) < 4:
            return
        if (data[0] & 0xC0) == 0x00:  # STUN
            return
        if (data[0] & 0xC0) != 0x80:  # not RTP/SRTP
            return

        # Decrypt SRTP → RTP
        if self.srtp_rx:
            rtp = self.srtp_rx.unprotect(data)
            if rtp is None:
                if self.pkt_count == 0:
                    _LOGGER.warning("SRTP audio auth failed from %s (%dB)", addr, len(data))
                return
        else:
            rtp = data

        if (rtp[1] & 0x7F) != 0:  # not PCMU
            return
        cc = rtp[0] & 0x0F
        hlen = 12 + cc * 4
        if len(rtp) <= hlen:
            return
        # Forward decrypted RTP to AV ffmpeg port
        self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', FFMPEG_AV_AUDIO_PORT))
        payload = rtp[hlen:]
        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.info("First SRTP audio from %s (%dB)", addr, len(payload))
        pcm = ulaw_decode(payload)
        try:
            self.audio_buffer.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self.audio_buffer.get_nowait()
                self.audio_buffer.put_nowait(pcm)
            except Exception:
                pass

    def send_rtp(self, ulaw_payload: bytes):
        if not self.transport or not self.remote_addr:
            return
        self.rtp_seq = (self.rtp_seq + 1) & 0xFFFF
        self.rtp_ts = (self.rtp_ts + len(ulaw_payload)) & 0xFFFFFFFF
        header = struct.pack('!BBHII',
            0x80, 0, self.rtp_seq, self.rtp_ts, self.rtp_ssrc)
        rtp = header + ulaw_payload
        if self.srtp_tx:
            rtp = self.srtp_tx.protect(rtp)
        self.transport.sendto(rtp, self.remote_addr)

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)
        _LOGGER.debug("STUN Audio → %s", self.remote_addr)


class RTPVideoProtocol(asyncio.DatagramProtocol):
    """Video SRTP: decrypt → depacketize RTP H.264 → send NALs via WebSocket.
    No ffmpeg — direct pipeline like the official Vimar app."""

    REORDER_BUF_SIZE = 5  # Hold up to 5 packets for reordering (~30ms at 15fps)

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        self.pkt_count = 0
        self.srtp_rx: SRTPContext | None = None
        # FU-A reassembly buffer
        self._fua_buf = bytearray()
        self._fua_started = False
        self._fua_expected_seq = None  # Track RTP seq for FU-A continuity
        # Ordered NAL send queue — preserves SPS→PPS→IDR order
        self._nal_queue: asyncio.Queue | None = None
        self._nal_sender_task: asyncio.Task | None = None
        # SPS/PPS reorder buffer — hold IDR until SPS+PPS received
        self._last_sps = None
        self._last_pps = None
        self._sps_pps_sent = False  # True after first SPS+PPS pair sent
        self._pending_idr = None   # IDR waiting for SPS+PPS
        # RTP reorder buffer — fixes out-of-order UDP packets
        self._reorder_buf = {}  # seq -> payload
        self._next_seq = None   # next expected sequence number
        # Diagnostics
        self._srtp_fail = 0
        self._srtp_ok = 0
        self._nal_count = 0
        self._nal_types = {}  # type -> count

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.info("RTP Video ready on :%d", RTP_VIDEO_PORT)
        # Start ordered NAL sender
        loop = asyncio.get_event_loop()
        self._nal_queue = asyncio.Queue(maxsize=500)
        self._nal_sender_task = loop.create_task(self._nal_sender())

    def datagram_received(self, data, addr):
        if len(data) < 4:
            return
        if (data[0] & 0xC0) != 0x80:  # not RTP/SRTP
            return

        # Decrypt SRTP → plain RTP
        if self.srtp_rx:
            rtp = self.srtp_rx.unprotect(data)
            if rtp is None:
                self._srtp_fail += 1
                if self._srtp_fail <= 5 or self._srtp_fail % 100 == 0:
                    _LOGGER.warning("SRTP video auth FAIL #%d (pkt %dB)", self._srtp_fail, len(data))
                return
            self._srtp_ok += 1
        else:
            rtp = data

        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.info("First video RTP from %s (%dB)", addr, len(rtp))
        if self.pkt_count <= 3 or self.pkt_count % 200 == 0:
            _LOGGER.info("Video pkt #%d: %dB, srtp_ok=%d fail=%d nals=%d types=%s",
                         self.pkt_count, len(rtp), self._srtp_ok, self._srtp_fail,
                         self._nal_count, self._nal_types)

        # Parse RTP header
        cc = rtp[0] & 0x0F
        hlen = 12 + cc * 4
        seq = struct.unpack_from('!H', rtp, 2)[0]
        # Check for extension header
        if rtp[0] & 0x10:
            if len(rtp) < hlen + 4:
                return
            ext_len = struct.unpack_from('!H', rtp, hlen + 2)[0]
            hlen += 4 + ext_len * 4
        if len(rtp) <= hlen:
            return
        payload = rtp[hlen:]

        # RTP reorder buffer — hold packets briefly to fix out-of-order UDP
        self._reorder_buf[seq] = payload

        if self._next_seq is None:
            self._next_seq = seq

        # Emit all consecutive packets starting from _next_seq
        while self._next_seq in self._reorder_buf:
            p = self._reorder_buf.pop(self._next_seq)
            self._depacketize(p, self._next_seq)
            self._next_seq = (self._next_seq + 1) & 0xFFFF

        # If buffer grows too large, flush oldest to avoid stalling
        if len(self._reorder_buf) > self.REORDER_BUF_SIZE:
            # Find the lowest seq in buffer and emit from there
            while self._reorder_buf:
                if self._next_seq in self._reorder_buf:
                    p = self._reorder_buf.pop(self._next_seq)
                    self._depacketize(p, self._next_seq)
                    self._next_seq = (self._next_seq + 1) & 0xFFFF
                else:
                    # Skip missing packet
                    self._next_seq = (self._next_seq + 1) & 0xFFFF
                if len(self._reorder_buf) <= 1:
                    break

    def _depacketize(self, payload, seq):
        """Depacketize RTP H.264 payload → send NAL units via WebSocket."""
        if len(payload) < 1:
            return
        nal_type = payload[0] & 0x1F

        if 1 <= nal_type <= 23:
            # Single NAL unit — send directly with Annex B start code
            self._emit_nal(payload)

        elif nal_type == 24:  # STAP-A
            # Aggregation: multiple NALs packed together
            off = 1
            while off + 2 <= len(payload):
                nalu_size = struct.unpack_from('!H', payload, off)[0]
                off += 2
                if off + nalu_size > len(payload):
                    break
                self._emit_nal(payload[off:off + nalu_size])
                off += nalu_size

        elif nal_type == 28:  # FU-A
            # Fragmentation: one NAL split across packets
            if len(payload) < 2:
                return
            fu_header = payload[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            nal_unit_type = fu_header & 0x1F
            fragment = payload[2:]

            if start:
                # Reconstruct NAL header: F|NRI from original + type from FU
                nal_header = (payload[0] & 0xE0) | nal_unit_type
                if self._fua_started:
                    _LOGGER.debug("FU-A new start while prev incomplete (type=%d buf=%d)",
                                  nal_unit_type, len(self._fua_buf))
                self._fua_buf = bytearray([nal_header])
                self._fua_buf.extend(fragment)
                self._fua_started = True
                self._fua_expected_seq = (seq + 1) & 0xFFFF
                if nal_unit_type in (5, 7, 8) or self.pkt_count <= 20:
                    _LOGGER.info("FU-A START seq=%d nalType=%d fragSize=%d",
                                 seq, nal_unit_type, len(fragment))
            elif not self._fua_started:
                # FU-A continuation without start — dropped start packet
                if self.pkt_count <= 20:
                    _LOGGER.warning("FU-A middle/end without start: seq=%d nalType=%d end=%s",
                                    seq, nal_unit_type, end)
                return
            else:
                # Check sequence continuity — tolerate small gaps (1-3 missing pkts)
                if self._fua_expected_seq is not None and seq != self._fua_expected_seq:
                    gap = (seq - self._fua_expected_seq) & 0xFFFF
                    if gap > 5:
                        # Too many missing packets — discard entire NAL
                        _LOGGER.warning("FU-A seq gap: expected %d got %d (gap=%d), discarding",
                                        self._fua_expected_seq, seq, gap)
                        self._fua_buf = bytearray()
                        self._fua_started = False
                        self._fua_expected_seq = None
                        return
                    else:
                        # Small gap — keep going, the NAL might still decode
                        _LOGGER.debug("FU-A seq gap: expected %d got %d (gap=%d), continuing",
                                      self._fua_expected_seq, seq, gap)
                self._fua_buf.extend(fragment)
                self._fua_expected_seq = (seq + 1) & 0xFFFF

            if end and self._fua_started:
                completed_type = self._fua_buf[0] & 0x1F if self._fua_buf else 0
                if completed_type in (5, 7, 8) or self._nal_count <= 20:
                    _LOGGER.info("FU-A END seq=%d nalType=%d totalSize=%d",
                                 seq, completed_type, len(self._fua_buf))
                self._emit_nal(bytes(self._fua_buf))
                self._fua_buf = bytearray()
                self._fua_started = False
                self._fua_expected_seq = None

    def _emit_nal(self, nal_data):
        """Queue a complete NAL unit for ordered sending via WebSocket.

        Ensures SPS→PPS→IDR ordering: if IDR arrives before SPS+PPS,
        buffer it and emit after both parameter sets are received.
        """
        if not nal_data or not ws_send_bytes or not self._nal_queue:
            return
        nal_type = nal_data[0] & 0x1F if nal_data else 0
        self._nal_count += 1
        self._nal_types[nal_type] = self._nal_types.get(nal_type, 0) + 1
        if self._nal_count <= 10 or nal_type in (7, 8, 5):
            _LOGGER.info("NAL #%d type=%d size=%d (first4: %s)",
                         self._nal_count, nal_type, len(nal_data),
                         nal_data[:4].hex() if len(nal_data) >= 4 else nal_data.hex())

        # Reorder: ensure SPS+PPS always precede IDR
        if nal_type == 7:  # SPS
            self._last_sps = nal_data
            # If we have both SPS+PPS now, emit them + any pending IDR
            if self._last_pps is not None:
                self._flush_params_and_idr()
            return
        elif nal_type == 8:  # PPS
            self._last_pps = nal_data
            if self._last_sps is not None:
                self._flush_params_and_idr()
            return
        elif nal_type == 5:  # IDR
            if not self._sps_pps_sent:
                # No SPS+PPS sent yet — buffer IDR
                _LOGGER.info("IDR buffered — waiting for SPS+PPS")
                self._pending_idr = nal_data
                return
            else:
                # Re-emit latest SPS+PPS before each IDR for robustness
                if self._last_sps:
                    self._queue_nal(self._last_sps)
                if self._last_pps:
                    self._queue_nal(self._last_pps)
        elif nal_type == 1:  # P-frame
            if not self._sps_pps_sent:
                return  # Drop P-frames before first IDR

        self._queue_nal(nal_data)

    def _flush_params_and_idr(self):
        """Emit SPS→PPS→(pending IDR) in correct order."""
        _LOGGER.info("Flushing SPS→PPS→IDR (pending_idr=%s)",
                     "yes" if self._pending_idr else "no")
        if self._last_sps:
            self._queue_nal(self._last_sps)
        if self._last_pps:
            self._queue_nal(self._last_pps)
        self._sps_pps_sent = True
        if self._pending_idr:
            self._queue_nal(self._pending_idr)
            self._pending_idr = None

    def _queue_nal(self, nal_data):
        """Low-level: queue NAL bytes to WebSocket send queue."""
        msg = b'\x03\x00\x00\x00\x01' + nal_data
        try:
            self._nal_queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                self._nal_queue.get_nowait()
                self._nal_queue.put_nowait(msg)
            except Exception:
                pass

    async def _nal_sender(self):
        """Drain NAL queue and send via WebSocket in order."""
        try:
            while True:
                msg = await self._nal_queue.get()
                if ws_send_bytes:
                    try:
                        await ws_send_bytes(msg)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)
        _LOGGER.debug("STUN Video → %s", self.remote_addr)


# ─── State ──────────────────────────────────────────────────────────

audio_proto: RTPAudioProtocol | None = None
video_proto: RTPVideoProtocol | None = None
av_ffmpeg_proc = None
_stun_task = None
_audio_task = None


# ─── Transport setup ────────────────────────────────────────────────

async def setup_transports():
    global audio_proto, video_proto
    loop = asyncio.get_event_loop()

    # Use SO_REUSEADDR to avoid "Address in use" on HA restart/reload
    audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    audio_sock.bind(('0.0.0.0', RTP_AUDIO_PORT))

    video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    video_sock.bind(('0.0.0.0', RTP_VIDEO_PORT))

    _, audio_proto = await loop.create_datagram_endpoint(
        RTPAudioProtocol, sock=audio_sock)
    _, video_proto = await loop.create_datagram_endpoint(
        RTPVideoProtocol, sock=video_sock)


async def setup_media(remote_sdp, local_crypto_key=None, local_video_crypto_key=None):
    """Start media after SIP call established. Called by sip.py."""
    global _stun_task, _audio_task
    audio = remote_sdp.get("audio", {})
    video = remote_sdp.get("video", {})
    remote_ip = remote_sdp.get("conn", "")

    remote_audio_key = audio.get("crypto_key")
    remote_video_key = video.get("crypto_key")

    if audio.get("port") and audio_proto:
        aip = audio.get("ip", remote_ip)
        audio_proto.remote_addr = (aip, audio["port"])
        audio_proto.pkt_count = 0
        if remote_audio_key:
            audio_proto.srtp_rx = SRTPContext(remote_audio_key)
            _LOGGER.info("SRTP Audio RX context created")
        if local_crypto_key:
            audio_proto.srtp_tx = SRTPContext(local_crypto_key)
            _LOGGER.info("SRTP Audio TX context created")
        audio_proto.send_stun()
        await broadcast("log", f"Audio SRTP → {aip}:{audio['port']}")

    if video.get("port") and video_proto:
        vip = video.get("ip", remote_ip)
        video_proto.remote_addr = (vip, video["port"])
        # Reset ALL state for new call
        video_proto.pkt_count = 0
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        video_proto._last_sps = None
        video_proto._last_pps = None
        video_proto._sps_pps_sent = False
        video_proto._pending_idr = None
        video_proto._reorder_buf = {}
        video_proto._next_seq = None
        video_proto._srtp_fail = 0
        video_proto._srtp_ok = 0
        video_proto._nal_count = 0
        video_proto._nal_types = {}
        if remote_video_key:
            video_proto.srtp_rx = SRTPContext(remote_video_key)
            _LOGGER.info("SRTP Video RX — direct H.264 depacketization (no ffmpeg)")
        video_proto.send_stun()
        await broadcast("log", f"Video SRTP → {vip}:{video['port']} (direct)")

    if _stun_task:
        _stun_task.cancel()
    _stun_task = asyncio.create_task(_stun_keepalive())

    if _audio_task:
        _audio_task.cancel()
    _audio_task = asyncio.create_task(_audio_broadcast())


async def stop_media():
    """Stop all media. Called on hangup/bye."""
    global _stun_task, _audio_task
    if _stun_task:
        _stun_task.cancel()
        _stun_task = None
    if _audio_task:
        _audio_task.cancel()
        _audio_task = None
    if audio_proto:
        audio_proto.remote_addr = None
        audio_proto.pkt_count = 0
        audio_proto.srtp_rx = None
        audio_proto.srtp_tx = None
        while not audio_proto.audio_buffer.empty():
            try:
                audio_proto.audio_buffer.get_nowait()
            except Exception:
                break
    if video_proto:
        video_proto.remote_addr = None
        video_proto.pkt_count = 0
        video_proto.srtp_rx = None
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
    await stop_av_ffmpeg()


def close_transports():
    """Close UDP transports — called on integration unload."""
    global audio_proto, video_proto
    if audio_proto and audio_proto.transport:
        audio_proto.transport.close()
        audio_proto = None
    if video_proto and video_proto.transport:
        video_proto.transport.close()
        video_proto = None


def send_audio(pcm_data: bytes):
    """Encode PCM from browser and send as RTP. Called by main.py."""
    ulaw = ulaw_encode(pcm_data)
    if audio_proto and audio_proto.remote_addr:
        audio_proto.send_rtp(ulaw)


# ─── STUN keepalive ─────────────────────────────────────────────────

async def _stun_keepalive():
    try:
        while True:
            await asyncio.sleep(15)
            if audio_proto and audio_proto.remote_addr:
                audio_proto.send_stun()
            if video_proto and video_proto.remote_addr:
                video_proto.send_stun()
    except asyncio.CancelledError:
        pass


# ─── Audio broadcast ────────────────────────────────────────────────

# ws_send_bytes: set by main.py — async fn(data) to send binary to all clients
ws_send_bytes = None


async def _audio_broadcast():
    """Forward decoded PCM to browser via WebSocket."""
    try:
        while True:
            if not audio_proto:
                await asyncio.sleep(0.5)
                continue
            try:
                pcm = await asyncio.wait_for(
                    audio_proto.audio_buffer.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if ws_send_bytes:
                await ws_send_bytes(b'\x01' + pcm)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _LOGGER.error("Audio broadcast error: %s", e)



# (ffmpeg video pipeline removed — H.264 NALs sent directly from RTPVideoProtocol)


# ─── AV stream (H264 video + PCMU audio → MPEG-TS for HomeKit) ────

def _create_av_sdp():
    """Create SDP with both video and audio for the AV ffmpeg."""
    sdp_path = "/tmp/intercom_av.sdp"
    with open(sdp_path, "w") as f:
        f.write(
            f"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=AV\r\n"
            f"c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {FFMPEG_AV_AUDIO_PORT} RTP/AVP 0\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"m=video {FFMPEG_AV_VIDEO_PORT} RTP/AVP 96\r\n"
            f"a=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 profile-level-id=42801F\r\n"
        )
    return sdp_path


async def start_av_ffmpeg():
    """Start ffmpeg that reads H264+PCMU RTP and outputs MPEG-TS to pipe."""
    global av_ffmpeg_proc
    await stop_av_ffmpeg()

    sdp_path = _create_av_sdp()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-protocol_whitelist", "file,udp,rtp",
        "-fflags", "+genpts+discardcorrupt",
        "-i", sdp_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-f", "mpegts",
        "pipe:1",
    ]
    try:
        av_ffmpeg_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        asyncio.create_task(_read_av_ffmpeg_stderr())
        _LOGGER.info("AV ffmpeg started (MPEG-TS output)")
    except Exception as e:
        _LOGGER.error("AV ffmpeg start error: %s", e)


async def stop_av_ffmpeg():
    global av_ffmpeg_proc
    if av_ffmpeg_proc:
        try:
            av_ffmpeg_proc.terminate()
            await asyncio.get_event_loop().run_in_executor(None, av_ffmpeg_proc.wait, 3)
        except Exception:
            try:
                av_ffmpeg_proc.kill()
            except Exception:
                pass
        av_ffmpeg_proc = None
        _LOGGER.info("AV ffmpeg stopped")


async def _read_av_ffmpeg_stderr():
    loop = asyncio.get_event_loop()
    while av_ffmpeg_proc and av_ffmpeg_proc.poll() is None:
        try:
            line = await loop.run_in_executor(None, av_ffmpeg_proc.stderr.readline)
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                _LOGGER.debug("AV ffmpeg: %s", text)
        except Exception:
            break
