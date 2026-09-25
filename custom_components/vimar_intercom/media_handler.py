"""Vimar Intercom — Media: RTP transport, STUN, G.711 codec, video capture, audio."""

import asyncio
import logging
import os
import random
import socket
import struct
import subprocess
import tempfile

from .runtime import RuntimeConfig
from .srtp import SRTPContext

_LOGGER = logging.getLogger(__name__)

# Set once by the hub at start-up. The integration declares
# single_config_entry, so one module-global config is correct.
CFG: RuntimeConfig | None = None


def configure(cfg: RuntimeConfig) -> None:
    """Install the runtime configuration for this media handler."""
    global CFG
    CFG = cfg


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
        _LOGGER.debug("RTP Audio ready on :%d", CFG.rtp_audio_port)

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
        self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', CFG.av_audio_port))
        payload = rtp[hlen:]
        self.pkt_count += 1
        if self.pkt_count == 1:
            _LOGGER.debug("First SRTP audio from %s (%dB)", addr, len(payload))
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


ANNEX_B_START = b"\x00\x00\x00\x01"

NAL_TYPE_SLICE = 1
NAL_TYPE_IDR = 5
NAL_TYPE_SPS = 7
NAL_TYPE_PPS = 8

# Per-consumer backlog cap, in bytes, for a consumer whose write stalls
# transiently (e.g. ffmpeg under CPU contention on the reference fanless
# two-core 7 W box, or a hiccup on its own output). On the reference
# hardware the kernel pipe buffer is only tens of KB, so a hiccup lasting
# a fraction of a second is enough to fill it and turn a write into
# BlockingIOError; without a backlog the consumer would be dropped for
# the rest of the call over a stall it would otherwise have recovered
# from in milliseconds. Assuming a doorbell-camera-class H.264 stream at
# roughly 1 Mbps at the high end (~128 KB/s), 256 KB buys about 2 seconds
# of catch-up before giving up and dropping the consumer for good.
MAX_CONSUMER_BACKLOG_BYTES = 256 * 1024


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
        # consumer -> bytes still owed to it after a BlockingIOError.
        # Capped at MAX_CONSUMER_BACKLOG_BYTES; see that constant.
        self._backlog: dict = {}

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
        self._backlog.pop(consumer, None)

    def reset(self) -> None:
        """Forget consumers and cached state, e.g. when a call ends."""
        self._consumers.clear()
        self._backlog.clear()
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
        """Send one NAL (as Annex-B) to a consumer, honoring its backlog.

        A consumer with backlogged bytes never gets new data ahead of
        them — the pending bytes and the new NAL are written as one
        chunk, in that order, so a slow consumer's stream is never
        reordered. `BlockingIOError` means the write is only transient
        (a non-blocking pipe that could not accept everything right
        now): whatever did not go out is kept, up to
        `MAX_CONSUMER_BACKLOG_BYTES`, and retried on the next call. Any
        other exception means the sink is genuinely gone and is dropped
        immediately.
        """
        data = ANNEX_B_START + nal
        backlog = self._backlog.get(consumer)
        pending = backlog + data if backlog else data
        try:
            consumer(pending)
        except BlockingIOError as exc:
            # A well-behaved consumer sets `characters_written` to report
            # a partial write (as `_make_ffmpeg_consumer` does); one that
            # doesn't is assumed to have written nothing, so the whole
            # chunk is kept and retried.
            written = getattr(exc, "characters_written", None) or 0
            remainder = pending[written:]
            if len(remainder) > MAX_CONSUMER_BACKLOG_BYTES:
                self._drop_consumer(consumer, "its output could not keep up with the stream")
            else:
                self._backlog[consumer] = remainder
        except Exception:  # noqa: BLE001 - a dead sink must not stop the rest
            self._drop_consumer(consumer, "it stopped accepting data")
        else:
            self._backlog.pop(consumer, None)

    def _drop_consumer(self, consumer, reason: str) -> None:
        """Remove a consumer for good and say why, once, not per NAL."""
        self.remove_consumer(consumer)
        _LOGGER.warning("Dropping a video consumer: %s", reason)


video_registry = VideoStreamRegistry()


class RTPVideoProtocol(asyncio.DatagramProtocol):
    """Video SRTP: decrypt → depacketize RTP H.264 → hand complete NAL
    units to the video registry, which fans them out to consumers. The
    one consumer wired up today is the ffmpeg AV pipeline (its stdin)."""

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
        # RTP reorder buffer — fixes out-of-order UDP packets
        self._reorder_buf = {}  # seq -> payload
        self._next_seq = None   # next expected sequence number
        # Diagnostics
        self._srtp_fail = 0
        self._srtp_ok = 0
        self._nal_count = 0

    def connection_made(self, transport):
        self.transport = transport
        _LOGGER.debug("RTP Video ready on :%d", CFG.rtp_video_port)

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
            _LOGGER.debug("First video RTP from %s (%dB)", addr, len(rtp))
        if self.pkt_count <= 3 or self.pkt_count % 200 == 0:
            _LOGGER.debug("Video pkt #%d: %dB, srtp_ok=%d fail=%d nals=%d",
                         self.pkt_count, len(rtp), self._srtp_ok, self._srtp_fail,
                         self._nal_count)

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
                if nal_unit_type in (5, 7, 8):
                    _LOGGER.debug("FU-A START seq=%d nalType=%d fragSize=%d",
                                 seq, nal_unit_type, len(fragment))
            elif not self._fua_started:
                # FU-A continuation without start — dropped start packet
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
                if completed_type in (5, 7, 8):
                    _LOGGER.debug("FU-A END seq=%d nalType=%d totalSize=%d",
                                 seq, completed_type, len(self._fua_buf))
                self._emit_nal(bytes(self._fua_buf))
                self._fua_buf = bytearray()
                self._fua_started = False
                self._fua_expected_seq = None

    def _emit_nal(self, nal_data):
        """Hand a complete NAL unit to the video registry."""
        if not nal_data:
            return
        self._nal_count += 1
        video_registry.push_nal(nal_data)

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
_av_sdp_path: str | None = None
_av_consumer = None


# ─── Transport setup ────────────────────────────────────────────────

async def setup_transports():
    global audio_proto, video_proto
    loop = asyncio.get_event_loop()

    # Use SO_REUSEADDR to avoid "Address in use" on HA restart/reload
    audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    audio_sock.bind(('0.0.0.0', CFG.rtp_audio_port))

    video_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    video_sock.bind(('0.0.0.0', CFG.rtp_video_port))

    _, audio_proto = await loop.create_datagram_endpoint(
        RTPAudioProtocol, sock=audio_sock)
    _, video_proto = await loop.create_datagram_endpoint(
        RTPVideoProtocol, sock=video_sock)


async def setup_media(remote_sdp, local_crypto_key=None, local_video_crypto_key=None):
    """Start media after SIP call established. Called by sip.py."""
    global _stun_task
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
            _LOGGER.debug("SRTP Audio RX context created")
        if local_crypto_key:
            audio_proto.srtp_tx = SRTPContext(local_crypto_key)
            _LOGGER.debug("SRTP Audio TX context created")
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
        video_proto._reorder_buf = {}
        video_proto._next_seq = None
        video_proto._srtp_fail = 0
        video_proto._srtp_ok = 0
        video_proto._nal_count = 0
        if remote_video_key:
            video_proto.srtp_rx = SRTPContext(remote_video_key)
            _LOGGER.debug("SRTP Video RX — direct H.264 depacketization (no ffmpeg)")
        video_proto.send_stun()
        await broadcast("log", f"Video SRTP → {vip}:{video['port']} (direct)")

    if _stun_task:
        _stun_task.cancel()
    _stun_task = asyncio.create_task(_stun_keepalive())


async def stop_media():
    """Stop all media. Called on hangup/bye."""
    global _stun_task
    if _stun_task:
        _stun_task.cancel()
        _stun_task = None
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
    video_registry.reset()


def close_transports():
    """Close UDP transports — called on integration unload."""
    global audio_proto, video_proto
    if audio_proto and audio_proto.transport:
        audio_proto.transport.close()
        audio_proto = None
    if video_proto and video_proto.transport:
        video_proto.transport.close()
        video_proto = None


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


# (ffmpeg video pipeline removed — H.264 NALs sent directly from RTPVideoProtocol)


# ─── AV stream (H264 video + PCMU audio → MPEG-TS for HomeKit) ────

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
        # Clean up the SDP we just wrote: this is the one exit path that
        # never reaches stop_av_ffmpeg.
        _LOGGER.error("Could not start ffmpeg: %s", err)
        av_ffmpeg_proc = None
        _cleanup_av_sdp()
        return

    # Bound how long a stalled ffmpeg can hold up the event loop: a
    # blocking write to a full pipe would stall push_nal, and with it
    # every RTP datagram this process receives. Non-blocking mode caps
    # the exposure at the kernel pipe buffer (tens of KB) and turns a
    # full pipe into BlockingIOError, which _send_one already treats
    # like any other dead consumer.
    os.set_blocking(av_ffmpeg_proc.stdin.fileno(), False)

    asyncio.create_task(_read_av_ffmpeg_stderr())
    _av_consumer = _make_ffmpeg_consumer(av_ffmpeg_proc)
    video_registry.add_consumer(_av_consumer)
    _LOGGER.info("AV pipeline started")


def _make_ffmpeg_consumer(proc):
    """Return a consumer that writes Annex-B NALs into ffmpeg's stdin.

    Writes go straight to the pipe's file descriptor with `os.write`
    rather than through the buffered file object's `.write()`, which
    would silently re-buffer a short write inside Python's io stack and
    hide exactly how much got through — the registry's backlog needs
    that exact count to avoid duplicating or dropping bytes. ffmpeg's
    stdin is put in non-blocking mode in `start_av_ffmpeg`, so a stall
    surfaces here as `BlockingIOError` (full pipe, nothing written) or
    as a short write (some bytes accepted, the rest reported back to
    the registry via `.characters_written`).
    """
    def _write(data: bytes) -> None:
        if proc.poll() is not None or proc.stdin is None:
            raise BrokenPipeError("ffmpeg has exited")
        try:
            written = os.write(proc.stdin.fileno(), data)
        except BlockingIOError:
            written = 0
        if written < len(data):
            err = BlockingIOError()
            err.characters_written = written
            raise err
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

    _cleanup_av_sdp()


def _cleanup_av_sdp() -> None:
    """Remove the temporary SDP file, if one is still on disk."""
    global _av_sdp_path
    if _av_sdp_path:
        try:
            os.unlink(_av_sdp_path)
        except OSError:
            pass
        _av_sdp_path = None


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
