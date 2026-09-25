"""Vimar Intercom — Media: RTP/SRTP transport, STUN, H.264 depacketisation, AV pipeline."""

import asyncio
import logging
import os
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


# ─── RTP Protocols ──────────────────────────────────────────────────

class RTPAudioProtocol(asyncio.DatagramProtocol):
    """Audio SRTP: decrypt, then forward the plain RTP to the AV ffmpeg port.

    Nothing on the receive path logs. `datagram_received` runs 50 times a
    second for the whole of a call, and a log statement there — at any
    level — is what produced the million-line log this rewrite was
    promised to fix. Everything worth knowing is counted here and
    reported once per call by `stop_media`.
    """

    def __init__(self):
        self.transport = None
        self.remote_addr = None
        self.pkt_count = 0
        self.srtp_fail = 0
        self.srtp_rx: SRTPContext | None = None
        # Forward decrypted RTP to the local port ffmpeg reads audio from.
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def connection_made(self, transport):
        self.transport = transport

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
                self.srtp_fail += 1
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
        self.pkt_count += 1

    def stats(self) -> str:
        """One-line summary of what this protocol saw during the call."""
        return f"audio pkts={self.pkt_count} srtp_fail={self.srtp_fail}"

    def close(self) -> None:
        """Release the forwarding socket."""
        try:
            self.ffmpeg_av_sock.close()
        except OSError:
            pass

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)


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
        self._last_keyframe: bytes | None = None
        # consumer -> bytes still owed to it after a BlockingIOError.
        # Capped at MAX_CONSUMER_BACKLOG_BYTES; see that constant.
        self._backlog: dict = {}

    @property
    def parameter_sets(self) -> tuple[bytes, bytes] | None:
        """The cached (SPS, PPS) pair, or None if not seen yet."""
        if self._sps is None or self._pps is None:
            return None
        return self._sps, self._pps

    @property
    def last_keyframe(self) -> bytes | None:
        """The most recent decodable keyframe, as an Annex-B byte string.

        SPS, PPS and the IDR they describe, concatenated — everything a
        decoder needs to produce one picture and nothing else. The
        camera entity turns this into a still, which is why a snapshot
        never has to open a stream or place a call of its own.
        """
        return self._last_keyframe

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
        self._last_keyframe = None

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
            self._cache_keyframe(nal)
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
            self._cache_keyframe(self._pending_idr)
            self._broadcast_parameter_sets()
            self._started = True
            self._broadcast(self._pending_idr)
            self._pending_idr = None

    def _cache_keyframe(self, idr: bytes) -> None:
        """Keep the parameter sets and this IDR as a self-contained still."""
        pair = self.parameter_sets
        if pair is None:
            return
        self._last_keyframe = b"".join(
            ANNEX_B_START + nal for nal in (*pair, idr))

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
    one consumer wired up today is the ffmpeg AV pipeline (its stdin).

    Like `RTPAudioProtocol`, nothing on the receive path logs: it runs
    around a hundred times a second, and a single lost packet mid
    keyframe would otherwise emit one line per remaining fragment. The
    events that used to be logged individually are counted here and
    summarised once per call by `stop_media`.
    """

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
        self.reset_counters()

    def reset_counters(self) -> None:
        """Zero the per-call diagnostic counters."""
        self._srtp_fail = 0
        self._srtp_ok = 0
        self._nal_count = 0
        self._fua_restarts = 0   # a new FU-A start while one was in flight
        self._fua_orphans = 0    # a continuation whose start packet was lost
        self._fua_discards = 0   # a NAL thrown away over too large a gap
        self._fua_gaps = 0       # a small gap ridden out

    def connection_made(self, transport):
        self.transport = transport

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
                return
            self._srtp_ok += 1
        else:
            rtp = data

        self.pkt_count += 1

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
        """Depacketize RTP H.264 payload → hand NAL units to the video registry."""
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
                    self._fua_restarts += 1
                self._fua_buf = bytearray([nal_header])
                self._fua_buf.extend(fragment)
                self._fua_started = True
                self._fua_expected_seq = (seq + 1) & 0xFFFF
            elif not self._fua_started:
                # FU-A continuation without start — dropped start packet
                self._fua_orphans += 1
                return
            else:
                # Check sequence continuity — tolerate small gaps (1-3 missing pkts)
                if self._fua_expected_seq is not None and seq != self._fua_expected_seq:
                    gap = (seq - self._fua_expected_seq) & 0xFFFF
                    if gap > 5:
                        # Too many missing packets — discard entire NAL
                        self._fua_discards += 1
                        self._fua_buf = bytearray()
                        self._fua_started = False
                        self._fua_expected_seq = None
                        return
                    # Small gap — keep going, the NAL might still decode
                    self._fua_gaps += 1
                self._fua_buf.extend(fragment)
                self._fua_expected_seq = (seq + 1) & 0xFFFF

            if end and self._fua_started:
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

    def stats(self) -> str:
        """One-line summary of what this protocol saw during the call."""
        return (f"video pkts={self.pkt_count} srtp_ok={self._srtp_ok} "
                f"srtp_fail={self._srtp_fail} nals={self._nal_count} "
                f"fua_restarts={self._fua_restarts} "
                f"fua_orphans={self._fua_orphans} "
                f"fua_discards={self._fua_discards} "
                f"fua_gaps={self._fua_gaps}")

    def send_stun(self):
        if not self.transport or not self.remote_addr:
            return
        stun = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + os.urandom(12)
        self.transport.sendto(stun, self.remote_addr)


# ─── State ──────────────────────────────────────────────────────────

audio_proto: RTPAudioProtocol | None = None
video_proto: RTPVideoProtocol | None = None
av_ffmpeg_proc = None
_stun_task = None
_av_sdp_path: str | None = None
_av_consumer = None
# One asyncio.Queue per attached AV viewer. The pipeline is started when
# the list goes from empty to non-empty and stopped when it empties
# again, so a second dashboard joins the running pipeline instead of
# killing the first viewer's ffmpeg out from under it.
_av_subscribers: list[asyncio.Queue] = []
_av_reader_task: asyncio.Task | None = None


# ─── Transport setup ────────────────────────────────────────────────

def _bind_udp(port: int) -> socket.socket:
    """Bind one UDP socket, closing it again if the bind fails."""
    # SO_REUSEADDR avoids "Address in use" on a Home Assistant reload.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', port))
    except OSError:
        sock.close()
        raise
    return sock


async def setup_transports():
    """Bind the RTP sockets, leaving nothing open if any step fails."""
    global audio_proto, video_proto
    loop = asyncio.get_event_loop()

    audio_sock = _bind_udp(CFG.rtp_audio_port)
    try:
        video_sock = _bind_udp(CFG.rtp_video_port)
    except OSError:
        # The audio socket is already bound at this point; a raise here
        # used to leak it, and one leaked socket per failed setup keeps
        # its port busy and makes every later attempt fail too.
        audio_sock.close()
        raise

    try:
        _, audio_proto = await loop.create_datagram_endpoint(
            RTPAudioProtocol, sock=audio_sock)
    except OSError:
        audio_sock.close()
        video_sock.close()
        raise

    try:
        _, video_proto = await loop.create_datagram_endpoint(
            RTPVideoProtocol, sock=video_sock)
    except OSError:
        video_sock.close()
        close_transports()
        raise


async def setup_media(remote_sdp):
    """Start media after SIP call established. Called by sip_client."""
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
        audio_proto.srtp_fail = 0
        if remote_audio_key:
            audio_proto.srtp_rx = SRTPContext(remote_audio_key)
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
        video_proto.reset_counters()
        if remote_video_key:
            video_proto.srtp_rx = SRTPContext(remote_video_key)
        video_proto.send_stun()
        await broadcast("log", f"Video SRTP → {vip}:{video['port']}")

    if _stun_task:
        _stun_task.cancel()
    _stun_task = asyncio.create_task(_stun_keepalive())


async def stop_media():
    """Stop all media. Called on hangup/bye."""
    global _stun_task
    if _stun_task:
        _stun_task.cancel()
        _stun_task = None

    # The one and only place the media layer reports what it saw: one
    # DEBUG line for a whole call, instead of a line per packet.
    if audio_proto or video_proto:
        _LOGGER.debug(
            "Call media summary: %s | %s",
            audio_proto.stats() if audio_proto else "audio absent",
            video_proto.stats() if video_proto else "video absent")

    if audio_proto:
        audio_proto.remote_addr = None
        audio_proto.pkt_count = 0
        audio_proto.srtp_fail = 0
        audio_proto.srtp_rx = None
    if video_proto:
        video_proto.remote_addr = None
        video_proto.pkt_count = 0
        video_proto.srtp_rx = None
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        video_proto.reset_counters()
    await stop_av_ffmpeg()
    video_registry.reset()


def close_transports():
    """Close UDP transports — called on integration unload."""
    global audio_proto, video_proto
    if audio_proto:
        if audio_proto.transport:
            audio_proto.transport.close()
        # The forwarding socket is not owned by the transport, so
        # closing the transport alone leaked one UDP socket per reload.
        audio_proto.close()
        audio_proto = None
    if video_proto:
        if video_proto.transport:
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


# ─── AV stream (H.264 video + PCMU audio → MPEG-TS) ─────────────────

# Read size for ffmpeg's MPEG-TS output: 4 KB is ~21 transport packets,
# small enough to keep latency low and large enough to keep the executor
# round-trips down.
AV_CHUNK_BYTES = 4096
# Per-viewer buffering before the slowest viewer starts losing chunks.
# 256 * 4 KB is 1 MB, roughly eight seconds of a 1 Mbps stream.
AV_QUEUE_CHUNKS = 256


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
    """Start ffmpeg muxing H.264 from stdin and audio RTP into MPEG-TS.

    Idempotent: a pipeline that is already running is left alone, which
    is what makes `av_subscribe` safe to call for a second viewer.
    """
    global av_ffmpeg_proc, _av_consumer, _av_reader_task
    if av_ffmpeg_proc is not None:
        if av_ffmpeg_proc.poll() is None:
            return
        # It exited on its own. Clear the consumer and reader it left
        # behind before putting a second set in their place.
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
    _av_reader_task = asyncio.create_task(_read_av_ffmpeg_stdout())
    _av_consumer = _make_ffmpeg_consumer(av_ffmpeg_proc)
    video_registry.add_consumer(_av_consumer)
    _LOGGER.info("AV pipeline started")


async def av_subscribe() -> asyncio.Queue | None:
    """Attach a viewer to the AV pipeline, starting it if needed.

    Returns the queue the viewer should read MPEG-TS chunks from, or
    None when ffmpeg could not be started. A `None` item on the queue
    means the pipeline has ended and the viewer should stop.
    """
    if av_ffmpeg_proc is None or av_ffmpeg_proc.poll() is not None:
        await start_av_ffmpeg()
        if av_ffmpeg_proc is None:
            return None
    queue: asyncio.Queue = asyncio.Queue(maxsize=AV_QUEUE_CHUNKS)
    _av_subscribers.append(queue)
    return queue


async def av_unsubscribe(queue: asyncio.Queue) -> None:
    """Detach a viewer, stopping the pipeline once the last one leaves."""
    if queue in _av_subscribers:
        _av_subscribers.remove(queue)
    if not _av_subscribers:
        await stop_av_ffmpeg()


async def _read_av_ffmpeg_stdout() -> None:
    """Fan ffmpeg's MPEG-TS output out to every attached viewer.

    One reader for the process, not one per viewer: two viewers reading
    the same pipe from separate executor threads would each get half of
    the transport stream and neither would decode.
    """
    loop = asyncio.get_running_loop()
    proc = av_ffmpeg_proc
    try:
        while proc and proc.poll() is None:
            chunk = await loop.run_in_executor(
                None, proc.stdout.read, AV_CHUNK_BYTES)
            if not chunk:
                break
            for queue in list(_av_subscribers):
                try:
                    queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    # This viewer's HTTP connection cannot keep up.
                    # Drop its oldest chunk rather than stall the
                    # others; MPEG-TS resynchronises on its own.
                    try:
                        queue.get_nowait()
                        queue.put_nowait(chunk)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a dead pipe must not kill the loop
        _LOGGER.debug("AV output reader stopped early", exc_info=True)
    finally:
        # Only when this reader still owns the live pipeline. A teardown
        # cancels the reader after detaching, and has already released
        # that pipeline's viewers; signalling again from here would hit
        # whoever has since subscribed to the replacement.
        if proc is not None and proc is av_ffmpeg_proc:
            _signal_av_end()


def _signal_av_end(queues: list[asyncio.Queue] | None = None) -> None:
    """Tell viewers the pipeline has finished.

    With no argument this is every currently attached viewer. A teardown
    passes the list it detached instead, so a viewer that subscribed to
    the next pipeline while this one was being reaped is left alone.
    """
    for queue in list(_av_subscribers if queues is None else queues):
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            # The sentinel matters more than the last chunk of video.
            try:
                queue.get_nowait()
                queue.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


async def snapshot_jpeg(timeout: float = 5.0) -> bytes | None:
    """Decode the most recent keyframe to a JPEG, or None if there is none.

    Deliberately independent of the streaming pipeline: a still must
    never start a call, so this reads the keyframe the registry already
    cached from a call that is running and decodes that one picture.
    """
    keyframe = video_registry.last_keyframe
    if not keyframe:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error",
            "-f", "h264", "-i", "pipe:0",
            "-frames:v", "1", "-f", "mjpeg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as err:
        _LOGGER.error("Could not start ffmpeg for a snapshot: %s", err)
        return None
    try:
        async with asyncio.timeout(timeout):
            out, _ = await proc.communicate(keyframe)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.debug("Snapshot decode timed out")
        return None
    except OSError as err:
        _LOGGER.debug("Snapshot decode failed: %s", err)
        return None
    return out or None


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
    """Stop the AV pipeline, detach it, and release every viewer.

    Everything this pipeline owns is taken out of the module globals
    synchronously, before the first await. Reaping ffmpeg takes up to
    three seconds, and `terminate()` does not make `poll()` return at
    once; a viewer arriving in that window used to see a process that
    still looked alive, join it, and be handed the end-of-stream
    sentinel by the teardown a moment later — an empty MPEG-TS body.
    Detaching first means such a viewer finds no pipeline and starts a
    fresh one.
    """
    global av_ffmpeg_proc, _av_consumer, _av_sdp_path, _av_reader_task

    proc, av_ffmpeg_proc = av_ffmpeg_proc, None
    consumer, _av_consumer = _av_consumer, None
    reader_task, _av_reader_task = _av_reader_task, None
    sdp_path, _av_sdp_path = _av_sdp_path, None
    # These viewers belong to the pipeline being torn down. Anyone who
    # subscribes from here on belongs to the next one and must not be
    # handed this one's sentinel.
    leaving = list(_av_subscribers)
    _av_subscribers.clear()

    if consumer is not None:
        video_registry.remove_consumer(consumer)

    if proc:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            await asyncio.get_running_loop().run_in_executor(
                None, proc.wait, 3)
        except Exception:  # noqa: BLE001 - the process may already be gone
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _LOGGER.info("AV pipeline stopped")

    # Cancel the reader only after the process is gone, so its blocking
    # read in the executor has already returned.
    if reader_task is not None:
        reader_task.cancel()

    _signal_av_end(leaving)
    _cleanup_av_sdp(sdp_path)


def _cleanup_av_sdp(path: str | None = None) -> None:
    """Remove a temporary SDP file, if one is still on disk.

    With no argument this is the current pipeline's. A teardown passes
    the path it detached, so it cannot delete the SDP of a pipeline that
    started while it was reaping the old one.
    """
    global _av_sdp_path
    if path is None:
        path, _av_sdp_path = _av_sdp_path, None
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


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
