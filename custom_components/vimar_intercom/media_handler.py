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
        self.rx_errors = 0
        self.srtp_rx: SRTPContext | None = None
        # Forward decrypted RTP to the local port ffmpeg reads audio from.
        self.ffmpeg_av_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Anything escaping a protocol callback is logged by asyncio's
        # default handler as a full traceback, once per datagram: 50 a
        # second, which is the flood the rest of this class is careful
        # to avoid. A reload clearing CFG, or a transient error from
        # sendto, is enough to reach it — so it is counted, not logged.
        try:
            self._receive(data)
        except Exception:  # noqa: BLE001 - counted here, reported once per call
            self.rx_errors += 1

    def _receive(self, data):
        cfg = CFG
        if cfg is None:
            return
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
        self.ffmpeg_av_sock.sendto(rtp, ('127.0.0.1', cfg.av_audio_port))
        self.pkt_count += 1

    def stats(self) -> str:
        """One-line summary of what this protocol saw during the call."""
        return (f"audio pkts={self.pkt_count} srtp_fail={self.srtp_fail} "
                f"rx_errors={self.rx_errors}")

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

    def add_consumer(self, consumer) -> bool:
        """Register a consumer, priming it so it can start decoding.

        The parameter sets alone are not enough: the very next thing a
        consumer attaching mid-stream receives is a predicted slice,
        which references a picture it never got, and the pipeline
        remuxes rather than decodes so nothing downstream notices —
        the viewer sees grey until the panel's next IDR, seconds away.
        The cached keyframe is SPS, PPS and the IDR they describe, so a
        late viewer starts on its first frame instead. It is already
        Annex-B framed, hence the raw write.

        Returns False if priming dropped the consumer straight away,
        which tells the caller its sink was dead before it began.
        """
        self._consumers.append(consumer)
        keyframe = self._last_keyframe
        if keyframe is not None:
            self._send_framed(consumer, keyframe)
        else:
            pair = self.parameter_sets
            if pair is not None:
                for nal in pair:
                    self._send_one(consumer, nal)
        return consumer in self._consumers

    def remove_consumer(self, consumer) -> None:
        """Stop sending to a consumer."""
        if consumer in self._consumers:
            self._consumers.remove(consumer)
        self._backlog.pop(consumer, None)

    def reset(self, *, keep_keyframe: bool = True) -> None:
        """Forget consumers and cached state, e.g. when a call ends.

        The last keyframe survives a call ending: for a doorbell the
        final frame of a call is the most valuable image there is, and
        clearing it left the camera entity with no still at all between
        calls. An unload passes `keep_keyframe=False`, because nothing
        should outlive the config entry it belongs to.
        """
        self._consumers.clear()
        self._backlog.clear()
        self._sps = None
        self._pps = None
        self._pending_idr = None
        self._started = False
        if not keep_keyframe:
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
        """Frame one NAL as Annex-B and send it to a consumer."""
        self._send_framed(consumer, ANNEX_B_START + nal)

    def _send_framed(self, consumer, data: bytes) -> None:
        """Send already-framed Annex-B bytes, honoring the backlog.

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
        self._fua_discards = 0   # a NAL thrown away over a gap
        self._late_pkts = 0      # arrived behind the window, dropped
        self._dup_pkts = 0       # already buffered, dropped
        self._resyncs = 0        # reorder buffer overflowed, window moved
        self.rx_errors = 0       # escaped the receive path, see below

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Same guard, and the same reason, as RTPAudioProtocol's: an
        # unhandled exception here is one asyncio traceback per datagram.
        try:
            self._receive(data)
        except Exception:  # noqa: BLE001 - counted here, reported once per call
            self.rx_errors += 1

    def _receive(self, data):
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
        if self._next_seq is None:
            self._next_seq = seq
        elif ((seq - self._next_seq) & 0xFFFF) > 0x8000:
            # Behind the window: a duplicate, or a late copy of a packet
            # the overflow drain already gave up on. Buffering it put a
            # key in the map that only a wrap of the whole 16-bit space
            # could remove, and the drain below then walked _next_seq
            # backwards over that space — inside this one callback, and
            # re-emitting every sequence number on the way.
            self._late_pkts += 1
            return
        if seq in self._reorder_buf:
            self._dup_pkts += 1
            return

        self._reorder_buf[seq] = payload

        self._drain_reorder_buf()

        # The packet the window is waiting for is late enough to be lost.
        # Resync to the oldest packet actually held — oldest in modular
        # terms, so a window that has just wrapped still finds it — which
        # bounds the work by the size of the buffer instead of by the
        # sequence space.
        if len(self._reorder_buf) > self.REORDER_BUF_SIZE:
            self._resyncs += 1
            self._next_seq = min(
                self._reorder_buf,
                key=lambda s: (s - self._next_seq) & 0xFFFF)
            self._drain_reorder_buf()

    def _drain_reorder_buf(self) -> None:
        """Emit every buffered packet consecutive from `_next_seq`."""
        while self._next_seq in self._reorder_buf:
            payload = self._reorder_buf.pop(self._next_seq)
            self._depacketize(payload, self._next_seq)
            self._next_seq = (self._next_seq + 1) & 0xFFFF

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
                # Any gap means a fragment of this NAL was lost, and the
                # pipeline remuxes rather than decodes: a slice with a
                # hole in it travels straight through to every client
                # decoder, where +discardcorrupt cannot see it. Riding
                # out small gaps bought artifacts and occasional decoder
                # desync; one discarded frame costs less. Reordering is
                # the reorder buffer's job, not this one's.
                if self._fua_expected_seq is not None and seq != self._fua_expected_seq:
                    self._fua_discards += 1
                    self._fua_buf = bytearray()
                    self._fua_started = False
                    self._fua_expected_seq = None
                    return
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
                f"late_pkts={self._late_pkts} dup_pkts={self._dup_pkts} "
                f"resyncs={self._resyncs} rx_errors={self.rx_errors}")

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
# The last still decoded, with the keyframe it came from: (keyframe, jpeg).
_snapshot_cache: tuple[bytes, bytes] | None = None

# Strong references to fire-and-forget tasks. asyncio keeps only a weak
# one, so a task nothing else holds can be collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Run a coroutine in the background and keep it alive until it ends."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# asyncio's synchronisation primitives bind to the event loop that first
# blocks on them and refuse to be used from any other one. Home
# Assistant has a single loop for the life of the process, so these are
# built once and reused there; rebuilding them happens only under a test
# that gives each case a loop of its own.
_sync_loop = None
_sync_primitives: dict = {}


def _per_loop(name: str, factory):
    """Return the named primitive, built for the running loop."""
    global _sync_loop
    loop = asyncio.get_running_loop()
    if loop is not _sync_loop:
        _sync_primitives.clear()
        _sync_loop = loop
    primitive = _sync_primitives.get(name)
    if primitive is None:
        primitive = _sync_primitives[name] = factory()
    return primitive


def _av_lifecycle_lock() -> asyncio.Lock:
    """The lock every change to the AV pipeline's lifetime is made under.

    The pipeline's lifetime lives in four module globals, and the rest
    of this file keeps them consistent by mutating them before its first
    await. `start_av_ffmpeg` cannot: it has to reap a process that
    exited on its own *before* it can spawn the replacement. Without
    this lock, a second viewer arriving during that reap found no
    process in the globals, started a pipeline of its own, and the first
    caller then overwrote every global with a third — leaving an ffmpeg
    nothing could ever stop, holding a pipe and a UDP port, and two
    readers interleaving their output into one viewer's stream.
    """
    return _per_loop("av_lifecycle", asyncio.Lock)


def _snapshot_slot() -> asyncio.Semaphore:
    """Admit one still decode at a time; the rest wait for its result."""
    return _per_loop("snapshot", lambda: asyncio.Semaphore(1))


# ─── Transport setup ────────────────────────────────────────────────

def _bind_udp(port: int) -> socket.socket:
    """Bind one UDP socket, closing it again if the bind fails."""
    # No SO_REUSEADDR: UDP has no TIME_WAIT, so the only way to get
    # EADDRINUSE on these ports is a socket of ours still being open.
    # Allowing the duplicate bind would turn that leak into unspecified
    # delivery between the two sockets — silent packet loss — instead of
    # the loud failure that says the close path did not run.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
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
        # Close what this call opened, and only that. Calling the unload
        # hook from here would also tear down whatever a previous,
        # successful setup left behind — a failure path has no business
        # depending on prior global state.
        video_sock.close()
        if audio_proto is not None:
            if audio_proto.transport:
                audio_proto.transport.close()
            audio_proto.close()
            audio_proto = None
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
        audio_proto.rx_errors = 0
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
        audio_proto.rx_errors = 0
        audio_proto.srtp_rx = None
    if video_proto:
        video_proto.remote_addr = None
        video_proto.pkt_count = 0
        video_proto.srtp_rx = None
        video_proto._fua_buf = bytearray()
        video_proto._fua_started = False
        video_proto._fua_expected_seq = None
        video_proto.reset_counters()
    # Under the lifecycle lock, and before the reap: reaping ffmpeg
    # takes up to three seconds, and a viewer arriving in that window
    # starts a replacement pipeline and registers its consumer.
    # Resetting afterwards cleared that consumer too, and the new ffmpeg
    # then sat on `pipe:0` receiving no NALs — it emitted nothing, the
    # reader produced no sentinel, and the viewer held a response body
    # that never arrived and never ended.
    async with _av_lifecycle_lock():
        video_registry.reset()
        detached = _detach_av_pipeline()
    await _reap_av_pipeline(*detached)


def close_transports():
    """Release everything the media layer holds — the unload hook.

    Everything, not just the two UDP transports: this is documented as
    *the* unload function, and a reload that left the keepalive looping,
    an ffmpeg holding the audio port the new instance is about to bind,
    a registry full of consumers pointing at the old pipeline and a
    `CFG` describing the unloaded entry is a reload that leaks all of
    them, once per reload, until Home Assistant restarts.

    Synchronous, because Home Assistant's unload calls it that way. The
    happy path has already awaited `stop_media`, so there is normally no
    pipeline left here; when there is, it is detached at once — nothing
    can join it after this returns — and reaped in the background.
    """
    global audio_proto, video_proto, _stun_task, CFG, _snapshot_cache

    if _stun_task:
        _stun_task.cancel()
        _stun_task = None

    detached = _detach_av_pipeline()
    if detached[0] is not None:
        try:
            _spawn(_reap_av_pipeline(*detached))
        except RuntimeError:
            # No running loop (a synchronous teardown in a test): kill
            # the child outright rather than leave it running.
            _kill_detached_pipeline(*detached)
    video_registry.reset(keep_keyframe=False)
    _snapshot_cache = None

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

    CFG = None


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
# How long ffmpeg may probe each input before it starts muxing, in
# microseconds, as ffmpeg wants it on the command line.
AV_ANALYZE_MICROSECONDS = "500000"


def _write_av_sdp(port: int) -> str:
    """Write the SDP that describes the audio RTP stream for ffmpeg.

    Blocking: the caller runs it in an executor. Home Assistant's
    blocking-I/O detector targets exactly a `mkstemp` on the loop.
    """
    fd, path = tempfile.mkstemp(prefix="vimar_av_", suffix=".sdp")
    with os.fdopen(fd, "w") as handle:
        handle.write(
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=AV\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
    return path


def _spawn_av_ffmpeg(cmd: list[str]):
    """Start the ffmpeg child. Blocking: forking is not loop work."""
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)


async def start_av_ffmpeg():
    """Start ffmpeg muxing H.264 from stdin and audio RTP into MPEG-TS.

    Idempotent: a pipeline that is already running is left alone, which
    is what makes `av_subscribe` safe to call for a second viewer.
    """
    async with _av_lifecycle_lock():
        await _start_av_pipeline()


async def _start_av_pipeline() -> None:
    """Start the pipeline. The lifecycle lock must already be held."""
    global av_ffmpeg_proc, _av_consumer, _av_reader_task, _av_sdp_path
    if av_ffmpeg_proc is not None:
        if av_ffmpeg_proc.poll() is None:
            return
        # It exited on its own. Reap what it left behind before putting
        # a second set of globals in its place — under the lock, because
        # this reap is the one await that happens with the globals in an
        # indeterminate state.
        await _reap_av_pipeline(*_detach_av_pipeline())

    loop = asyncio.get_running_loop()
    sdp_path = await loop.run_in_executor(None, _write_av_sdp, CFG.av_audio_port)
    # Raw H.264 on a pipe carries no timestamps, and `-fflags +genpts`
    # cannot invent them for a stream with no container: with `-c copy`
    # the MPEG-TS muxer rejected every packet as invalid data and the
    # viewer got the stream headers and nothing else. Stamping each
    # packet with the time it arrives is right for a live source anyway.
    # The short analyze window keeps ffmpeg from spending its default
    # five seconds probing both inputs before writing a byte, which is
    # half of the time a panel keeps an auto-on call open.
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", AV_ANALYZE_MICROSECONDS,
        "-use_wallclock_as_timestamps", "1",
        "-f", "h264", "-i", "pipe:0",
        "-protocol_whitelist", "file,udp,rtp",
        "-analyzeduration", AV_ANALYZE_MICROSECONDS,
        "-i", sdp_path,
        "-map", "0:v", "-map", "1:a",
        # Video is copied as it arrives. The panel's audio is G.711,
        # which MPEG-TS can only carry as an opaque data stream that no
        # browser plays, so it is encoded to AAC; at 8 kHz mono that
        # costs next to nothing.
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "32k",
        "-f", "mpegts",
        "pipe:1",
    ]
    try:
        proc = await loop.run_in_executor(None, _spawn_av_ffmpeg, cmd)
    except OSError as err:
        # Clean up the SDP we just wrote: this is the one exit path that
        # never reaches a teardown.
        _LOGGER.error("Could not start ffmpeg: %s", err)
        _cleanup_av_sdp(sdp_path)
        return

    # Bound how long a stalled ffmpeg can hold up the event loop: a
    # blocking write to a full pipe would stall push_nal, and with it
    # every RTP datagram this process receives. Non-blocking mode caps
    # the exposure at the kernel pipe buffer (tens of KB) and turns a
    # full pipe into BlockingIOError, which _send_one already treats
    # like any other dead consumer.
    os.set_blocking(proc.stdin.fileno(), False)

    # Claim the globals only now, all together and with nothing left to
    # await: from here a viewer joins this pipeline or none at all.
    av_ffmpeg_proc = proc
    _av_sdp_path = sdp_path
    _av_reader_task = _spawn(_read_av_ffmpeg_stdout(proc))
    _spawn(_read_av_ffmpeg_stderr(proc))
    _av_consumer = _make_ffmpeg_consumer(proc)
    if not video_registry.add_consumer(_av_consumer):
        # Its stdin was already refusing data. Without this the pipeline
        # would stay in the globals with no consumer feeding it, never
        # emit a byte, and no later viewer would ever get video.
        _LOGGER.warning("The AV pipeline's input closed as it started")
        await _reap_av_pipeline(*_detach_av_pipeline())
        return
    _LOGGER.info("AV pipeline started")


async def av_subscribe() -> asyncio.Queue | None:
    """Attach a viewer to the AV pipeline, starting it if needed.

    Returns the queue the viewer should read MPEG-TS chunks from, or
    None when ffmpeg could not be started. A `None` item on the queue
    means the pipeline has ended and the viewer should stop.
    """
    async with _av_lifecycle_lock():
        if av_ffmpeg_proc is None or av_ffmpeg_proc.poll() is not None:
            await _start_av_pipeline()
            if av_ffmpeg_proc is None:
                return None
        queue: asyncio.Queue = asyncio.Queue(maxsize=AV_QUEUE_CHUNKS)
        _av_subscribers.append(queue)
        return queue


async def av_unsubscribe(queue: asyncio.Queue) -> None:
    """Detach a viewer, stopping the pipeline once the last one leaves."""
    async with _av_lifecycle_lock():
        if queue in _av_subscribers:
            _av_subscribers.remove(queue)
        if _av_subscribers:
            return
        detached = _detach_av_pipeline()
    # Outside the lock: reaping takes up to three seconds, and a viewer
    # arriving meanwhile should start a fresh pipeline at once rather
    # than wait for this one's corpse.
    await _reap_av_pipeline(*detached)


async def _read_av_ffmpeg_stdout(proc) -> None:
    """Fan ffmpeg's MPEG-TS output out to every attached viewer.

    One reader for the process, not one per viewer: two viewers reading
    the same pipe from separate executor threads would each get half of
    the transport stream and neither would decode. The process is a
    parameter, not the global, so a reader can never end up reading the
    output of a pipeline that replaced the one it was started for.
    """
    loop = asyncio.get_running_loop()
    try:
        while True:
            # read1, not read: read() on a BufferedReader blocks until it
            # has the full 4 KB, which is 160 ms of added latency on a
            # 200 kbps stream. Reading until EOF rather than while
            # poll() is None also means the last buffered bytes of a
            # pipeline that exits normally still reach the viewers.
            chunk = await loop.run_in_executor(
                None, proc.stdout.read1, AV_CHUNK_BYTES)
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
            # ffmpeg ended on its own — audio RTP stopped, or a fatal
            # demuxer error. Take the dead handle out of the globals
            # here and now, synchronously, so the next viewer starts a
            # fresh pipeline instead of finding a corpse in them, and
            # reap the remains in the background. Leaving it there is
            # what made the ordinary reconnect spawn a second ffmpeg.
            dead, _, sdp_path, leaving = _detach_av_pipeline()
            _signal_av_end(leaving)
            _spawn(_reap_av_pipeline(dead, None, sdp_path, []))


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

    One decode at a time, and the result is kept: the cached keyframe is
    immutable, so every call between two IDRs would otherwise spawn an
    ffmpeg to produce a byte-identical JPEG — on the reference two-core
    7 W box that is a real fraction of a core per dashboard poll.
    """
    keyframe = video_registry.last_keyframe
    if not keyframe:
        return None

    global _snapshot_cache
    async with _snapshot_slot():
        cached = _snapshot_cache
        if cached is not None and cached[0] is keyframe:
            return cached[1]
        jpeg = await _decode_keyframe(keyframe, timeout)
        if jpeg is not None:
            _snapshot_cache = (keyframe, jpeg)
        return jpeg


async def _decode_keyframe(keyframe: bytes, timeout: float) -> bytes | None:
    """Run one keyframe through ffmpeg, leaving no child behind."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error",
            "-probesize", "32", "-analyzeduration", "0",
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
        _LOGGER.debug("Snapshot decode timed out")
        return None
    except OSError as err:
        _LOGGER.debug("Snapshot decode failed: %s", err)
        return None
    finally:
        # Every exit path, cancellation included: Home Assistant cancels
        # the request whenever a dashboard tab closes or navigates
        # mid-fetch, and without this the child survived it. kill() is
        # synchronous, so the process dies even if the wait below is
        # itself cancelled.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            else:
                await proc.wait()
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
    """Stop the AV pipeline, detach it, and release every viewer."""
    async with _av_lifecycle_lock():
        detached = _detach_av_pipeline()
    # The reap is deliberately outside the lock: it takes up to three
    # seconds, and a viewer arriving meanwhile must be able to start a
    # fresh pipeline at once instead of waiting for this one's corpse.
    await _reap_av_pipeline(*detached)


def _detach_av_pipeline():
    """Take everything the current pipeline owns out of the globals.

    Synchronous, so nothing can interleave: from the moment it returns,
    a viewer arriving finds no pipeline and starts a fresh one. Reaping
    ffmpeg takes up to three seconds and `terminate()` does not make
    `poll()` return at once, so a viewer arriving during the reap used
    to see a process that still looked alive, join it, and be handed the
    end-of-stream sentinel a moment later — an empty MPEG-TS body.

    Returns what the caller must then reap: the process, its stdout
    reader, its SDP file and the viewers it leaves behind.
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

    return proc, reader_task, sdp_path, leaving


async def _reap_av_pipeline(proc, reader_task, sdp_path, leaving,
                            cancel_reader: bool = True) -> None:
    """Wait for a detached pipeline to die and release what it held."""
    loop = asyncio.get_running_loop()

    if proc:
        try:
            # stdin first: ffmpeg finishes the mux on EOF, and the
            # terminate below is only there for one that does not.
            if proc.stdin is not None:
                proc.stdin.close()
            proc.terminate()
        except Exception:  # noqa: BLE001 - the process may already be gone
            pass
        try:
            await loop.run_in_executor(None, proc.wait, 3)
        except subprocess.TimeoutExpired:
            # terminate() was not enough. Killing without waiting again
            # leaves a zombie until the garbage collector happens to
            # reap it.
            proc.kill()
            try:
                await loop.run_in_executor(None, proc.wait, 3)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - a fake or already-reaped process
            pass

    # Cancel the reader only after the process is gone, so its blocking
    # read in the executor has already returned, and await it so this
    # cannot return while the reader is still in its own finally — or,
    # worse, close the pipe it is reading out from under it.
    if cancel_reader and reader_task is not None:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # our own cancellation, not the reader's
        except Exception:  # noqa: BLE001 - the reader reports its own errors
            pass

    if proc:
        # All three pipes, not just stdin: stdout and stderr are file
        # objects of ours, and leaving them to refcounting keeps two fds
        # per pipeline for as long as anything still references the
        # Popen — a parked reader thread, for instance.
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001 - closing a dead pipe may raise
                    pass
        _LOGGER.info("AV pipeline stopped")

    _signal_av_end(leaving)
    _cleanup_av_sdp(sdp_path)


def _kill_detached_pipeline(proc, reader_task, sdp_path, leaving) -> None:
    """Last-resort synchronous teardown, when there is no loop to reap on."""
    if proc:
        try:
            proc.kill()
            proc.wait(3)
        except Exception:  # noqa: BLE001 - the process may already be gone
            pass
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


async def _read_av_ffmpeg_stderr(proc) -> None:
    """Drain ffmpeg's stderr, counting its lines instead of logging them.

    One DEBUG line per warning was the same unbounded shape as the
    per-packet logging this file is careful to avoid: at -loglevel
    warning a lossy stream makes ffmpeg complain about corrupt data
    per frame, which is millions of lines a day at 15-30 fps. Counted
    here, summarised once when the pipeline ends. The first and last
    lines are what a bug report needs; the 200 000 between them are not.
    """
    loop = asyncio.get_running_loop()
    lines = 0
    first = None
    last = None
    while True:
        try:
            raw = await loop.run_in_executor(None, proc.stderr.readline)
        except Exception:  # noqa: BLE001 - a closed pipe ends the summary
            break
        if not raw:
            break
        text = raw.decode(errors="replace").strip()
        if not text:
            continue
        lines += 1
        if first is None:
            first = text
        last = text
    if lines:
        _LOGGER.debug("AV ffmpeg wrote %d stderr line(s); first: %s | last: %s",
                      lines, first, last)
