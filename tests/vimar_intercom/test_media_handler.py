"""Tests for the media layer's hot paths and its socket ownership.

media_handler.py imports no Home Assistant module, so it loads through
the stub package tests/conftest.py installs.
"""

import asyncio
import logging
import os
import socket
import struct
import threading

import pytest

from custom_components.vimar_intercom import media_handler as media
from custom_components.vimar_intercom import runtime

QR_FIELDS = {
    "ID": "60901",
    "PWD": "examplepassword",
    "CDOMAIN": "example.invalid",
}

SPS = bytes([0x67, 0x42, 0x80, 0x1F])
PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
IDR = bytes([0x65, 0x11, 0x22, 0x33])


def run(coro):
    """Run one coroutine to completion on a private event loop."""
    return asyncio.run(coro)


async def settle() -> None:
    """Let the module's background tasks finish before asserting."""
    while media._background_tasks:
        await asyncio.gather(*list(media._background_tasks),
                             return_exceptions=True)


@pytest.fixture
def cfg(monkeypatch):
    """Install a runtime config with ports nothing else will use."""
    config = runtime.build_runtime_config(
        runtime.entry_data_from_qr(QR_FIELDS), {"rtp_port_base": 21100})
    monkeypatch.setattr(media, "CFG", config)
    return config


def rtp_packet(seq: int, payload: bytes, payload_type: int = 96) -> bytes:
    """Build a minimal RTP packet carrying `payload`."""
    header = struct.pack("!BBHII", 0x80, payload_type, seq, 0, 0x1234)
    return header + payload


def fua_packet(seq: int, *, start: bool, end: bool,
               nal_type: int = 5, fragment: bytes = b"\x00" * 8) -> bytes:
    """Build an RTP packet carrying one FU-A fragment."""
    indicator = 0x60 | 28  # NRI from the original NAL, FU-A type
    fu_header = (0x80 if start else 0) | (0x40 if end else 0) | nal_type
    return rtp_packet(seq, bytes([indicator, fu_header]) + fragment)


# ─── the per-packet logging promise ──────────────────────────────────

def test_video_packet_loss_logs_nothing(cfg, caplog):
    """One lost start packet used to emit a WARNING per remaining fragment."""
    proto = media.RTPVideoProtocol()
    with caplog.at_level(logging.DEBUG, logger=media._LOGGER.name):
        for seq in range(1, 41):
            proto.datagram_received(
                fua_packet(seq, start=False, end=False), ("192.0.2.10", 5004))
    assert caplog.records == []
    assert proto._fua_orphans == 40


def test_a_healthy_video_stream_logs_nothing(cfg, caplog):
    proto = media.RTPVideoProtocol()
    with caplog.at_level(logging.DEBUG, logger=media._LOGGER.name):
        proto.datagram_received(rtp_packet(1, SPS), ("192.0.2.10", 5004))
        proto.datagram_received(rtp_packet(2, PPS), ("192.0.2.10", 5004))
        for seq in range(3, 500):
            proto.datagram_received(rtp_packet(seq, IDR), ("192.0.2.10", 5004))
    assert caplog.records == []
    assert proto.pkt_count == 499


def test_audio_packets_log_nothing_even_when_every_one_fails_auth(cfg, caplog):
    """`pkt_count` never advances on this path, so the old guard never shut up."""
    class _AlwaysFails:
        def unprotect(self, _data):
            return None

    proto = media.RTPAudioProtocol()
    proto.srtp_rx = _AlwaysFails()
    try:
        with caplog.at_level(logging.DEBUG, logger=media._LOGGER.name):
            for seq in range(200):
                proto.datagram_received(
                    rtp_packet(seq, b"\x00" * 160, payload_type=0),
                    ("192.0.2.10", 5004))
        assert caplog.records == []
        assert proto.srtp_fail == 200
    finally:
        proto.close()


def test_stop_media_reports_the_whole_call_in_one_line(cfg, caplog,
                                                       monkeypatch):
    audio = media.RTPAudioProtocol()
    video = media.RTPVideoProtocol()
    monkeypatch.setattr(media, "audio_proto", audio)
    monkeypatch.setattr(media, "video_proto", video)
    try:
        video.datagram_received(
            fua_packet(1, start=False, end=False), ("192.0.2.10", 5004))
        with caplog.at_level(logging.DEBUG, logger=media._LOGGER.name):
            run(media.stop_media())
        summaries = [r for r in caplog.records
                     if "Call media summary" in r.getMessage()]
        assert len(summaries) == 1
        assert summaries[0].levelno == logging.DEBUG
        assert "fua_orphans=1" in summaries[0].getMessage()
    finally:
        audio.close()


def test_an_unloaded_entry_does_not_raise_once_per_packet(cfg, caplog,
                                                          monkeypatch):
    """`CFG` is dereferenced on the audio path and cleared on unload.

    Anything escaping a protocol callback is logged by asyncio's default
    handler as a full traceback, once per datagram — 50 a second, which
    is the million-line log this rewrite was published to fix.
    """
    monkeypatch.setattr(media, "CFG", None)
    proto = media.RTPAudioProtocol()
    try:
        with caplog.at_level(logging.DEBUG):
            for seq in range(200):
                proto.datagram_received(
                    rtp_packet(seq, b"\x00" * 160, payload_type=0),
                    ("192.0.2.10", 5004))
        assert caplog.records == []
        assert proto.pkt_count == 0
    finally:
        proto.close()


def test_a_failing_forward_is_counted_not_logged(cfg, caplog):
    """A dead forwarding socket raises OSError on every single packet."""
    proto = media.RTPAudioProtocol()
    proto.close()  # the forwarding socket is gone; sendto will raise
    with caplog.at_level(logging.DEBUG):
        for seq in range(200):
            proto.datagram_received(
                rtp_packet(seq, b"\x00" * 160, payload_type=0),
                ("192.0.2.10", 5004))
    assert caplog.records == []
    assert proto.rx_errors == 200
    assert "rx_errors=200" in proto.stats()


# ─── the RTP reorder buffer ──────────────────────────────────────────

def recorder(proto):
    """Record the sequence numbers the protocol releases, in order."""
    seen: list[int] = []
    proto._depacketize = lambda payload, seq: seen.append(seq)
    return seen


def feed(proto, *seqs: int) -> None:
    """Deliver one video packet per sequence number given."""
    for seq in seqs:
        proto.datagram_received(rtp_packet(seq, IDR), ("192.0.2.10", 5004))


def test_packets_in_order_are_released_once_each():
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, *range(100, 106))
    assert seen == list(range(100, 106))
    assert proto._reorder_buf == {}
    assert proto._next_seq == 106
    assert proto.rx_errors == 0


def test_an_out_of_order_packet_is_put_back_in_place():
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, 100, 102, 101)
    assert seen == [100, 101, 102]
    assert proto._reorder_buf == {}


def test_a_late_packet_is_dropped_instead_of_buffered():
    """Buffered, its key could only leave the map when the window wrapped
    the whole 16-bit space — and the overflow drain walked it there."""
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, 100, 101, 102, 101)
    assert seen == [100, 101, 102]
    assert proto._reorder_buf == {}
    assert proto._late_pkts == 1


def test_a_duplicate_of_a_held_packet_is_counted_not_held_twice():
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, 100, 102, 102)
    assert seen == [100]
    assert list(proto._reorder_buf) == [102]
    assert proto._dup_pkts == 1


def test_the_window_wraps_past_65535():
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, 65534, 65535, 0, 1)
    assert seen == [65534, 65535, 0, 1]
    assert proto._next_seq == 2


def test_reordering_still_works_across_the_wrap():
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, 65534, 0, 65535)
    assert seen == [65534, 65535, 0]
    assert proto._next_seq == 1
    assert proto._late_pkts == 0


def test_a_lost_packet_resyncs_the_window_to_the_oldest_held_one():
    """Stepping the window forward one sequence number at a time made
    the cost of a loss the distance to the next packet, not the size of
    the buffer."""
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)
    feed(proto, 100)          # 101 is lost
    feed(proto, *range(102, 108))
    assert seen == [100, *range(102, 108)]
    assert proto._next_seq == 108
    assert proto._resyncs == 1
    assert proto._reorder_buf == {}


def test_a_late_packet_never_rewinds_the_window():
    """The pathological case, reproduced: a loss, then the lost packet
    arriving too late to use, twice over, and a third loss after them.

    Each stale key used to stay in the buffer until the window wrapped
    round to it, and the overflow drain walked it there one sequence
    number at a time — 65 000 iterations inside a single UDP callback,
    leaving the window thousands of packets *behind* the live stream and
    re-releasing sequence numbers it had already released.
    """
    proto = media.RTPVideoProtocol()
    seen = recorder(proto)

    feed(proto, 1000)                    # 1001 lost
    feed(proto, *range(1002, 1008))      # overflow: resync past it
    feed(proto, 1001)                    # arrives far too late

    feed(proto, *range(1009, 1015))      # 1008 lost, overflow again
    feed(proto, 1008)                    # too late again

    feed(proto, *range(1016, 1022))      # 1015 lost, overflow again

    assert seen == [1000, *range(1002, 1008),
                    *range(1009, 1015), *range(1016, 1022)]
    assert seen == sorted(seen), "the window went backwards"
    assert len(seen) == len(set(seen)), "a sequence number was released twice"
    assert proto._next_seq == 1022
    assert proto._late_pkts == 2
    assert proto.rx_errors == 0


def test_a_gap_inside_a_fragmented_nal_discards_it():
    """The pipeline remuxes rather than decodes, so a slice with a hole
    in it reaches every client decoder with nothing able to see it."""
    proto = media.RTPVideoProtocol()
    proto.datagram_received(fua_packet(1, start=True, end=False),
                            ("192.0.2.10", 5004))
    # Packet 2 is lost; 3 arrives, and the window releases it once the
    # reorder buffer gives up on 2.
    for seq in range(3, 10):
        proto.datagram_received(fua_packet(seq, start=False, end=(seq == 9)),
                                ("192.0.2.10", 5004))
    assert proto._fua_discards == 1
    assert proto._nal_count == 0
    assert proto._fua_started is False


# ─── the keyframe a still is decoded from ────────────────────────────

def test_the_registry_caches_a_self_contained_keyframe():
    registry = media.VideoStreamRegistry()
    assert registry.last_keyframe is None
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    assert registry.last_keyframe == b"".join(
        media.ANNEX_B_START + nal for nal in (SPS, PPS, IDR))


def test_an_idr_arriving_before_its_parameter_sets_is_still_cached():
    registry = media.VideoStreamRegistry()
    registry.push_nal(IDR)
    assert registry.last_keyframe is None
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    assert registry.last_keyframe == b"".join(
        media.ANNEX_B_START + nal for nal in (SPS, PPS, IDR))


def test_the_keyframe_outlives_the_call_it_came_from():
    """For a doorbell the last frame of a call is the most valuable
    image there is; clearing it left the camera with no still at all
    between calls, while its docstring promised one."""
    registry = media.VideoStreamRegistry()
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.reset()
    assert registry.last_keyframe == b"".join(
        media.ANNEX_B_START + nal for nal in (SPS, PPS, IDR))
    # The rest of the call state does go.
    assert registry.parameter_sets is None
    assert registry._consumers == []


def test_an_unload_forgets_the_keyframe():
    """Nothing should outlive the config entry it belongs to."""
    registry = media.VideoStreamRegistry()
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.reset(keep_keyframe=False)
    assert registry.last_keyframe is None


def test_no_keyframe_means_no_snapshot(monkeypatch):
    registry = media.VideoStreamRegistry()
    monkeypatch.setattr(media, "video_registry", registry)
    assert run(media.snapshot_jpeg()) is None


# ─── decoding that still ─────────────────────────────────────────────

class _FakeChild:
    """Enough of an asyncio subprocess for snapshot_jpeg."""

    def __init__(self, output=b"jpeg", delay=0.0, fail=None):
        self.returncode = None
        self.killed = False
        self._output = output
        self._delay = delay
        self._fail = fail

    async def communicate(self, _input=None):
        if self._fail is not None:
            raise self._fail
        await asyncio.sleep(self._delay)
        self.returncode = 0
        return self._output, b""

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def fake_decoder(monkeypatch):
    """Stand in for the ffmpeg a snapshot spawns, and count the spawns."""
    children: list[_FakeChild] = []
    settings: dict = {"output": b"jpeg", "delay": 0.0, "fail": None}

    async def _exec(*_args, **_kwargs):
        child = _FakeChild(settings["output"], settings["delay"],
                           settings["fail"])
        children.append(child)
        return child

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(media, "_snapshot_cache", None)
    return children, settings


def _registry_with_keyframe(monkeypatch):
    registry = media.VideoStreamRegistry()
    for nal in (SPS, PPS, IDR):
        registry.push_nal(nal)
    monkeypatch.setattr(media, "video_registry", registry)
    return registry


def test_a_cancelled_snapshot_leaves_no_ffmpeg_behind(fake_decoder,
                                                      monkeypatch):
    """Home Assistant cancels the request whenever a dashboard tab
    closes or navigates mid-fetch: an every-day event, and the child
    outlived every one of them."""
    children, settings = fake_decoder
    settings["delay"] = 30
    _registry_with_keyframe(monkeypatch)

    async def scenario():
        task = asyncio.create_task(media.snapshot_jpeg())
        await asyncio.sleep(0.05)  # let it reach communicate()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    assert len(children) == 1
    assert children[0].killed, "the aborted decode left a child running"


def test_a_snapshot_that_errors_leaves_no_ffmpeg_behind(fake_decoder,
                                                        monkeypatch):
    """The OSError branch returned without touching the child at all."""
    children, settings = fake_decoder
    settings["fail"] = OSError("write to a closed pipe")
    _registry_with_keyframe(monkeypatch)

    assert run(media.snapshot_jpeg()) is None
    assert children[0].killed


def test_simultaneous_snapshots_decode_once_and_share_the_result(
        fake_decoder, monkeypatch):
    """A dashboard polling the thumbnail spawned one ffmpeg per poll for
    a picture that is bit-identical until the next IDR — real CPU on a
    two-core 7 W box, for nothing."""
    children, settings = fake_decoder
    settings["delay"] = 0.02
    _registry_with_keyframe(monkeypatch)

    async def scenario():
        return await asyncio.gather(*(media.snapshot_jpeg()
                                      for _ in range(12)))

    results = run(scenario())
    assert results == [b"jpeg"] * 12
    assert len(children) == 1


def test_a_new_keyframe_is_decoded_again(fake_decoder, monkeypatch):
    """The cache must follow the picture, not outlive it."""
    children, settings = fake_decoder
    registry = _registry_with_keyframe(monkeypatch)

    async def scenario():
        first = await media.snapshot_jpeg()
        settings["output"] = b"jpeg2"
        cached = await media.snapshot_jpeg()
        registry.push_nal(IDR)  # a new keyframe, a new picture
        return first, cached, await media.snapshot_jpeg()

    first, cached, refreshed = run(scenario())
    assert (first, cached, refreshed) == (b"jpeg", b"jpeg", b"jpeg2")
    assert len(children) == 2


# ─── socket ownership ────────────────────────────────────────────────

def test_setup_transports_leaves_nothing_open_when_the_video_bind_fails(cfg):
    """The audio socket used to leak, keeping its own port busy for good."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", cfg.rtp_video_port))
    try:
        with pytest.raises(OSError):
            run(media.setup_transports())
        # The audio port must be free again; binding it proves it.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("0.0.0.0", cfg.rtp_audio_port))
        finally:
            probe.close()
    finally:
        blocker.close()


def test_close_transports_closes_the_audio_forwarding_socket(cfg, monkeypatch):
    """It is not owned by the transport, so it leaked once per reload."""
    audio = media.RTPAudioProtocol()
    monkeypatch.setattr(media, "audio_proto", audio)
    monkeypatch.setattr(media, "video_proto", None)
    forwarder = audio.ffmpeg_av_sock

    media.close_transports()

    assert media.audio_proto is None
    assert forwarder.fileno() == -1


# ─── AV pipeline reference counting ──────────────────────────────────

class _FakeProc:
    """Enough of a Popen for the subscriber bookkeeping."""

    def poll(self):
        return None


@pytest.fixture
def fake_av(monkeypatch):
    """Pretend ffmpeg starts and is reaped, and count both.

    Only the two ends of the pipeline's life are stubbed: the lifecycle
    lock, the detach and the subscriber bookkeeping are the real ones,
    so these tests still run the code that keeps them consistent.
    """
    events: list[str] = []
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_consumer", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", None)

    async def _start():
        events.append("start")
        media.av_ffmpeg_proc = _FakeProc()

    async def _reap(proc, reader_task, sdp_path, leaving,
                    cancel_reader=True):
        if proc is not None:
            events.append("stop")
        media._signal_av_end(leaving)

    monkeypatch.setattr(media, "_start_av_pipeline", _start)
    monkeypatch.setattr(media, "_reap_av_pipeline", _reap)
    return events


def test_a_second_viewer_joins_the_running_pipeline(fake_av):
    """It used to kill the first viewer's ffmpeg and split its output."""
    async def scenario():
        first = await media.av_subscribe()
        second = await media.av_subscribe()
        assert first is not None and second is not None
        assert first is not second
        return first, second

    first, second = run(scenario())
    assert fake_av == ["start"]
    assert media._av_subscribers == [first, second]


def test_the_pipeline_stops_only_when_the_last_viewer_leaves(fake_av):
    async def scenario():
        first = await media.av_subscribe()
        second = await media.av_subscribe()
        await media.av_unsubscribe(first)
        assert fake_av == ["start"]
        await media.av_unsubscribe(second)

    run(scenario())
    assert fake_av == ["start", "stop"]
    assert media._av_subscribers == []


class _ChunkProc:
    """A Popen whose stdout hands out a fixed list of chunks, then EOF."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.stdout = self
        self.stdin = None
        self.stderr = None
        self.reaped = False

    def poll(self):
        return None if self._chunks else 0

    def read1(self, _size):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        pass

    def terminate(self):
        pass

    def kill(self):  # pragma: no cover - only on the error path
        pass

    def wait(self, timeout=None):
        self.reaped = True
        return 0


def _drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


@pytest.fixture
def detached_av(monkeypatch):
    """Isolate the module's AV globals for a test that drives the reader."""
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_consumer", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", None)


def test_the_reader_fans_every_chunk_out_to_every_viewer(detached_av,
                                                         monkeypatch):
    """The real fan-out: one reader on ffmpeg's stdout, N viewer queues.

    Two viewers reading that pipe themselves would each get half of the
    transport stream and neither would decode.
    """
    proc = _ChunkProc([b"one", b"two", b"three"])
    monkeypatch.setattr(media, "av_ffmpeg_proc", proc)

    async def scenario():
        first: asyncio.Queue = asyncio.Queue(maxsize=media.AV_QUEUE_CHUNKS)
        second: asyncio.Queue = asyncio.Queue(maxsize=media.AV_QUEUE_CHUNKS)
        media._av_subscribers.extend([first, second])
        await media._read_av_ffmpeg_stdout(proc)
        drained = _drain(first), _drain(second)
        await settle()
        return drained

    assert run(scenario()) == (
        [b"one", b"two", b"three", None],
        [b"one", b"two", b"three", None],
    )


def test_a_pipeline_that_ends_on_its_own_leaves_no_dead_handle(detached_av,
                                                               monkeypatch):
    """ffmpeg exiting by itself — audio RTP stops, a fatal demuxer error —
    used to leave a dead process in the globals. The next viewer then
    found it, awaited its reap, and a viewer arriving during *that*
    started a second pipeline nothing could ever stop."""
    proc = _ChunkProc([b"one"])
    monkeypatch.setattr(media, "av_ffmpeg_proc", proc)
    monkeypatch.setattr(media, "_av_consumer", lambda data: None)
    registry = media.VideoStreamRegistry()
    registry.add_consumer(media._av_consumer)
    monkeypatch.setattr(media, "video_registry", registry)

    async def scenario():
        viewer: asyncio.Queue = asyncio.Queue(maxsize=4)
        media._av_subscribers.append(viewer)
        await media._read_av_ffmpeg_stdout(proc)
        await settle()
        return _drain(viewer)

    assert run(scenario()) == [b"one", None]
    assert media.av_ffmpeg_proc is None
    assert media._av_consumer is None
    assert media._av_subscribers == []
    assert registry._consumers == []
    assert proc.reaped


def test_a_viewer_that_cannot_keep_up_loses_its_oldest_chunk(detached_av,
                                                             monkeypatch):
    """Its backlog must not stall the viewers that are keeping up."""
    proc = _ChunkProc([b"one", b"two", b"three"])
    monkeypatch.setattr(media, "av_ffmpeg_proc", proc)

    async def scenario():
        slow: asyncio.Queue = asyncio.Queue(maxsize=2)
        fast: asyncio.Queue = asyncio.Queue(maxsize=media.AV_QUEUE_CHUNKS)
        media._av_subscribers.extend([slow, fast])
        await media._read_av_ffmpeg_stdout(proc)
        drained = _drain(slow), _drain(fast)
        await settle()
        return drained

    slow, fast = run(scenario())
    # The slow viewer keeps the newest chunks it has room for, and the
    # sentinel displaces one more: the pipeline ending matters most.
    assert slow == [b"three", None]
    assert fast == [b"one", b"two", b"three", None]


def test_ending_the_pipeline_releases_every_viewer(fake_av):
    """The sentinel is what lets a blocked HTTP handler return."""
    async def scenario():
        first = await media.av_subscribe()
        second = await media.av_subscribe()
        media._signal_av_end()
        return first.get_nowait(), second.get_nowait()

    assert run(scenario()) == (None, None)


# ─── a viewer arriving while the pipeline is being torn down ─────────

class _ReapedProc:
    """A Popen that stays alive to `poll()` until its wait is released.

    That is the real shape: `terminate()` is a signal, and `poll()` goes
    on returning None until the process actually dies.
    """

    def __init__(self, gate):
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self._gate = gate

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):  # pragma: no cover - only on the error path
        pass

    def wait(self, timeout=None):
        self._gate.wait(timeout)
        return 0


def test_a_viewer_arriving_during_teardown_starts_a_fresh_pipeline(
        monkeypatch):
    """It used to join the dying one and get its end-of-stream sentinel.

    `stop_av_ffmpeg` yields for up to three seconds reaping ffmpeg, and
    `poll()` still returns None throughout. A viewer subscribing in that
    window took the "already running" branch, and the teardown then
    pushed `None` into its brand-new queue: the HTTP handler broke out
    at once and returned an empty MPEG-TS body.
    """
    gate = threading.Event()
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_consumer", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", _ReapedProc(gate))

    started: list[str] = []

    async def _start():
        started.append("start")
        media.av_ffmpeg_proc = _FakeProc()

    monkeypatch.setattr(media, "_start_av_pipeline", _start)

    async def scenario():
        leaving: asyncio.Queue = asyncio.Queue(maxsize=4)
        media._av_subscribers.append(leaving)

        stopping = asyncio.create_task(media.stop_av_ffmpeg())
        await asyncio.sleep(0.05)  # let it reach the executor wait

        newcomer = await media.av_subscribe()

        gate.set()
        await stopping
        return leaving, newcomer

    leaving, newcomer = run(scenario())

    assert started == ["start"]
    assert newcomer is not None
    assert newcomer.empty(), "the newcomer was handed the old pipeline's end"
    assert _drain(leaving) == [None]
    assert media._av_subscribers == [newcomer]
    assert media.av_ffmpeg_proc is not None


def test_a_pipeline_started_during_teardown_keeps_its_consumer(monkeypatch):
    """The call-ended teardown must not strip the pipeline after it.

    `stop_media` awaits `stop_av_ffmpeg` — up to three seconds reaping
    ffmpeg — and then reset the registry. A viewer arriving in that
    window starts a replacement pipeline and registers its consumer, and
    the reset cleared that one too: the new ffmpeg sat on `pipe:0`
    receiving no NALs, emitted nothing, so its reader never produced the
    end-of-stream sentinel and the viewer held a response body that
    never arrived and never ended.
    """
    gate = threading.Event()
    registry = media.VideoStreamRegistry()
    monkeypatch.setattr(media, "video_registry", registry)
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "_stun_task", None)
    monkeypatch.setattr(media, "audio_proto", None)
    monkeypatch.setattr(media, "video_proto", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", _ReapedProc(gate))

    leaving: list[bytes] = []
    arriving: list[bytes] = []
    monkeypatch.setattr(media, "_av_consumer", leaving.append)
    registry.add_consumer(leaving.append)

    async def _start():
        media.av_ffmpeg_proc = _FakeProc()
        media._av_consumer = arriving.append
        media.video_registry.add_consumer(media._av_consumer)

    monkeypatch.setattr(media, "_start_av_pipeline", _start)

    async def scenario():
        stopping = asyncio.create_task(media.stop_media())
        await asyncio.sleep(0.05)  # let it reach the executor wait
        newcomer = await media.av_subscribe()
        gate.set()
        await stopping
        return newcomer

    newcomer = run(scenario())
    assert newcomer is not None

    for nal in (SPS, PPS, IDR):
        media.video_registry.push_nal(nal)

    assert arriving == [media.ANNEX_B_START + nal for nal in (SPS, PPS, IDR)]
    assert leaving == []


# ─── a second viewer arriving while the pipeline is restarting ───────

class _DeadProc:
    """A Popen that has already exited, and whose reap takes a while.

    That is the shape the reconnect leaves behind: ffmpeg exits on its
    own, the reader's sentinel goes out, and a dead handle sits in the
    globals until somebody reaps it.
    """

    def __init__(self, gate):
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self._gate = gate

    def poll(self):
        return 1

    def terminate(self):
        pass

    def kill(self):  # pragma: no cover - only on the error path
        pass

    def wait(self, timeout=None):
        self._gate.wait(timeout)
        return 1


class _LiveProc:
    """A stand-in for a running ffmpeg, with a real pipe behind stdin."""

    def __init__(self):
        read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        self.stdin = os.fdopen(write_fd, "wb", buffering=0)
        self.stdout = None
        self.stderr = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):  # pragma: no cover - only on the error path
        pass

    def wait(self, timeout=None):
        return 0

    def release(self):
        self.stdin.close()
        os.close(self._read_fd)


def test_two_viewers_restarting_a_dead_pipeline_get_exactly_one_ffmpeg(
        cfg, monkeypatch):
    """`start_av_ffmpeg` is the one place that has to await with the
    globals in an indeterminate state: finding a dead process, it reaps
    it before spawning the replacement.

    A second viewer arriving in that window saw no process in the
    globals, took the fresh-start path, and the first caller then
    overwrote all four globals with a third pipeline. What was left was
    an ffmpeg nothing could ever stop — holding pipe:0 and a UDP port,
    one more per reconnect until Home Assistant restarts — two stdout
    readers interleaving their output into every viewer's stream, an
    orphaned registry consumer and a leaked SDP file.
    """
    gate = threading.Event()
    registry = media.VideoStreamRegistry()
    monkeypatch.setattr(media, "video_registry", registry)
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_consumer", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", _DeadProc(gate))

    spawned: list[_LiveProc] = []
    sdp_paths: list[str] = []
    real_write_sdp = media._write_av_sdp

    def _spawn_proc(_cmd):
        spawned.append(_LiveProc())
        return spawned[-1]

    def _write_sdp(port):
        sdp_paths.append(real_write_sdp(port))
        return sdp_paths[-1]

    async def _no_reader(_proc):
        return None

    monkeypatch.setattr(media, "_spawn_av_ffmpeg", _spawn_proc)
    monkeypatch.setattr(media, "_write_av_sdp", _write_sdp)
    monkeypatch.setattr(media, "_read_av_ffmpeg_stdout", _no_reader)
    monkeypatch.setattr(media, "_read_av_ffmpeg_stderr", _no_reader)

    async def scenario():
        # The reap finishes on its own, after both viewers have arrived.
        asyncio.get_running_loop().call_later(0.05, gate.set)
        first, second = await asyncio.gather(media.av_subscribe(),
                                             media.av_subscribe())
        await settle()
        return first, second

    try:
        first, second = run(scenario())

        assert len(spawned) == 1, "a second ffmpeg was spawned and orphaned"
        assert media.av_ffmpeg_proc is spawned[0]
        assert first is not None and second is not None
        assert media._av_subscribers == [first, second]
        assert len(registry._consumers) == 1
        assert media._av_sdp_path == sdp_paths[-1]
        leaked = [p for p in sdp_paths
                  if p != media._av_sdp_path and os.path.exists(p)]
        assert leaked == [], "an SDP file was left on disk"
    finally:
        for proc in spawned:
            proc.release()
        for path in sdp_paths:
            if os.path.exists(path):
                os.unlink(path)


# ─── what an unload has to release ───────────────────────────────────

def test_unload_releases_the_keepalive_the_pipeline_and_the_config(
        cfg, monkeypatch):
    """`close_transports` is documented as *the* unload hook, and used to
    release two UDP transports and nothing else: the STUN keepalive
    looped forever, ffmpeg survived the reload holding the audio port
    the new instance was about to bind, the registry kept consumers
    pointing at the old pipeline, and CFG still described the entry that
    had just gone away."""
    gate = threading.Event()
    gate.set()  # nothing to wait for; the reap returns at once
    registry = media.VideoStreamRegistry()
    for nal in (SPS, PPS, IDR):
        registry.push_nal(nal)
    monkeypatch.setattr(media, "video_registry", registry)
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "_snapshot_cache", None)
    monkeypatch.setattr(media, "video_proto", None)

    audio = media.RTPAudioProtocol()
    monkeypatch.setattr(media, "audio_proto", audio)
    proc = _ReapedProc(gate)
    monkeypatch.setattr(media, "av_ffmpeg_proc", proc)
    consumer = registry._consumers.append  # a sink that accepts anything
    monkeypatch.setattr(media, "_av_consumer", consumer)
    registry.add_consumer(consumer)

    async def scenario():
        media._stun_task = asyncio.create_task(media._stun_keepalive())
        await asyncio.sleep(0)
        stun_task = media._stun_task
        viewer: asyncio.Queue = asyncio.Queue(maxsize=4)
        media._av_subscribers.append(viewer)

        media.close_transports()

        await settle()
        return stun_task, viewer

    stun_task, viewer = run(scenario())

    assert stun_task.cancelled() or stun_task.done()
    assert media._stun_task is None
    assert media.av_ffmpeg_proc is None
    assert media._av_consumer is None
    assert media._av_subscribers == []
    assert _drain(viewer) == [None]
    assert registry._consumers == []
    assert registry.last_keyframe is None
    assert media.audio_proto is None
    assert media.CFG is None


# ─── ffmpeg's own diagnostics ────────────────────────────────────────

class _NoisyStderr:
    """A stderr pipe that produces one warning per frame, then EOF."""

    def __init__(self, count):
        self._remaining = count

    def readline(self):
        if self._remaining <= 0:
            return b""
        self._remaining -= 1
        return b"[h264 @ 0x1] error while decoding MB 4 20, bytestream -7\n"


def test_ffmpeg_warnings_are_summarised_once_not_logged_per_frame(caplog):
    """At -loglevel warning a lossy stream makes ffmpeg complain per
    frame: one DEBUG line each is 1.3-2.6 M lines a day, the same order
    as the incident this component's logging rules exist to prevent."""
    proc = _ChunkProc([])
    proc.stderr = _NoisyStderr(5000)

    with caplog.at_level(logging.DEBUG, logger=media._LOGGER.name):
        run(media._read_av_ffmpeg_stderr(proc))
    caplog.records[:] = [r for r in caplog.records
                         if r.name == media._LOGGER.name]

    assert len(caplog.records) == 1
    assert "5000" in caplog.records[0].getMessage()
    assert caplog.records[0].levelno == logging.DEBUG


# ─── the ffmpeg command line ─────────────────────────────────────────

def test_the_video_input_is_stamped_with_arrival_time(cfg, monkeypatch):
    """Raw H.264 on a pipe has no timestamps. Copied into MPEG-TS without
    them, every packet was refused as invalid data and a viewer received
    the stream headers and nothing else, while the call itself carried
    video. The flags only apply to the input that follows them, so their
    position matters as much as their presence.
    """
    registry = media.VideoStreamRegistry()
    monkeypatch.setattr(media, "video_registry", registry)
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "_av_consumer", None)
    monkeypatch.setattr(media, "_av_sdp_path", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", None)

    spawned: list[_LiveProc] = []
    commands: list[list[str]] = []

    def _spawn_proc(cmd):
        commands.append(cmd)
        spawned.append(_LiveProc())
        return spawned[-1]

    async def _no_reader(_proc):
        return None

    monkeypatch.setattr(media, "_spawn_av_ffmpeg", _spawn_proc)
    monkeypatch.setattr(media, "_read_av_ffmpeg_stdout", _no_reader)
    monkeypatch.setattr(media, "_read_av_ffmpeg_stderr", _no_reader)

    try:
        run(media.start_av_ffmpeg())
        [cmd] = commands
        video_input = cmd.index("pipe:0")
        audio_input = cmd.index(media._av_sdp_path)
        before_video = cmd[:video_input]
        between = cmd[video_input:audio_input]

        assert "-use_wallclock_as_timestamps" in before_video
        assert before_video[before_video.index(
            "-use_wallclock_as_timestamps") + 1] == "1"
        assert "-analyzeduration" in before_video
        assert "-analyzeduration" in between
        # G.711 in MPEG-TS is a data stream no browser plays.
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-c:v") + 1] == "copy"
    finally:
        sdp = media._av_sdp_path
        run(media.stop_av_ffmpeg())
        for proc in spawned:
            proc.release()
        if sdp and os.path.exists(sdp):
            os.unlink(sdp)


# ─── the audio this side sends ───────────────────────────────────────

class _RecordingTransport:
    def __init__(self):
        self.sent: list[tuple[bytes, tuple]] = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


def test_a_call_sends_the_panel_silent_audio_until_it_ends(cfg, monkeypatch):
    """A call carrying no media from this side was ended by the far end
    after about ten seconds, cutting every auto-on view short. The client
    sends muted-microphone silence, encrypted under the key it offered,
    for as long as the call lasts.
    """
    import base64

    from custom_components.vimar_intercom.srtp import SRTPContext

    key = base64.b64encode(os.urandom(30)).decode()
    audio = media.RTPAudioProtocol()
    transport = _RecordingTransport()
    audio.transport = transport
    monkeypatch.setattr(media, "audio_proto", audio)
    monkeypatch.setattr(media, "video_proto", None)
    monkeypatch.setattr(media, "local_audio_key", key)
    monkeypatch.setattr(media, "_stun_task", None)
    monkeypatch.setattr(media, "_silence_task", None)

    remote = {"audio": {"port": 40000, "ip": "192.0.2.20"}}

    async def scenario():
        await media.setup_media(remote)
        await asyncio.sleep(0.11)
        await media.stop_media()
        sent_during = len(transport.sent)
        await asyncio.sleep(0.06)
        return sent_during

    try:
        sent_during = run(scenario())
        # One STUN binding, then a packet every 20 ms.
        media_packets = [d for d, _ in transport.sent if d[0] & 0xC0 == 0x80]
        assert len(media_packets) >= 3
        assert len(transport.sent) == sent_during, "silence outlived the call"
        assert all(addr == ("192.0.2.20", 40000) for _, addr in transport.sent)

        rx = SRTPContext(key)
        first, second = (rx.unprotect(p) for p in media_packets[:2])
        assert first is not None and second is not None
        assert first[1] & 0x7F == 0  # PCMU
        assert first[12:] == b"\xff" * 160
        seq = struct.unpack_from("!H", first, 2)[0]
        assert struct.unpack_from("!H", second, 2)[0] == (seq + 1) & 0xFFFF
    finally:
        audio.close()


def test_no_offered_key_means_no_silence(cfg, monkeypatch):
    audio = media.RTPAudioProtocol()
    transport = _RecordingTransport()
    audio.transport = transport
    monkeypatch.setattr(media, "audio_proto", audio)
    monkeypatch.setattr(media, "video_proto", None)
    monkeypatch.setattr(media, "local_audio_key", None)
    monkeypatch.setattr(media, "_stun_task", None)
    monkeypatch.setattr(media, "_silence_task", None)

    async def scenario():
        await media.setup_media({"audio": {"port": 40000, "ip": "192.0.2.20"}})
        await asyncio.sleep(0.05)
        await media.stop_media()

    try:
        run(scenario())
        assert all(d[0] & 0xC0 != 0x80 for d, _ in transport.sent)
    finally:
        audio.close()
