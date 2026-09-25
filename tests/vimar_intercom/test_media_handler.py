"""Tests for the media layer's hot paths and its socket ownership.

media_handler.py imports no Home Assistant module, so it loads through
the stub package tests/conftest.py installs.
"""

import asyncio
import logging
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


def test_the_keyframe_is_forgotten_when_the_call_ends():
    registry = media.VideoStreamRegistry()
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.reset()
    assert registry.last_keyframe is None


def test_no_keyframe_means_no_snapshot(monkeypatch):
    registry = media.VideoStreamRegistry()
    monkeypatch.setattr(media, "video_registry", registry)
    assert run(media.snapshot_jpeg()) is None


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
    """Pretend ffmpeg starts and stops, and count both."""
    events: list[str] = []
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "_av_reader_task", None)
    monkeypatch.setattr(media, "av_ffmpeg_proc", None)

    async def _start():
        events.append("start")
        media.av_ffmpeg_proc = _FakeProc()

    async def _stop():
        if media.av_ffmpeg_proc is not None:
            events.append("stop")
            media.av_ffmpeg_proc = None
        media._signal_av_end()

    monkeypatch.setattr(media, "start_av_ffmpeg", _start)
    monkeypatch.setattr(media, "stop_av_ffmpeg", _stop)
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

    def poll(self):
        return None

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


def _drain(queue: asyncio.Queue) -> list:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_the_reader_fans_every_chunk_out_to_every_viewer(monkeypatch):
    """The real fan-out: one reader on ffmpeg's stdout, N viewer queues.

    Two viewers reading that pipe themselves would each get half of the
    transport stream and neither would decode.
    """
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "av_ffmpeg_proc",
                        _ChunkProc([b"one", b"two", b"three"]))

    async def scenario():
        first: asyncio.Queue = asyncio.Queue(maxsize=media.AV_QUEUE_CHUNKS)
        second: asyncio.Queue = asyncio.Queue(maxsize=media.AV_QUEUE_CHUNKS)
        media._av_subscribers.extend([first, second])
        await media._read_av_ffmpeg_stdout()
        return _drain(first), _drain(second)

    assert run(scenario()) == (
        [b"one", b"two", b"three", None],
        [b"one", b"two", b"three", None],
    )


def test_a_viewer_that_cannot_keep_up_loses_its_oldest_chunk(monkeypatch):
    """Its backlog must not stall the viewers that are keeping up."""
    monkeypatch.setattr(media, "_av_subscribers", [])
    monkeypatch.setattr(media, "av_ffmpeg_proc",
                        _ChunkProc([b"one", b"two", b"three"]))

    async def scenario():
        slow: asyncio.Queue = asyncio.Queue(maxsize=2)
        fast: asyncio.Queue = asyncio.Queue(maxsize=media.AV_QUEUE_CHUNKS)
        media._av_subscribers.extend([slow, fast])
        await media._read_av_ffmpeg_stdout()
        return _drain(slow), _drain(fast)

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

    monkeypatch.setattr(media, "start_av_ffmpeg", _start)

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

    monkeypatch.setattr(media, "start_av_ffmpeg", _start)

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
