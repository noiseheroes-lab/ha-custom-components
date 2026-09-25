"""Tests for the media layer's hot paths and its socket ownership.

media_handler.py imports no Home Assistant module, so it loads through
the stub package tests/conftest.py installs.
"""

import asyncio
import logging
import socket
import struct

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


def test_every_viewer_gets_every_chunk(fake_av):
    async def scenario():
        first = await media.av_subscribe()
        second = await media.av_subscribe()
        for queue in media._av_subscribers:
            queue.put_nowait(b"ts-chunk")
        return first.get_nowait(), second.get_nowait()

    assert run(scenario()) == (b"ts-chunk", b"ts-chunk")


def test_ending_the_pipeline_releases_every_viewer(fake_av):
    """The sentinel is what lets a blocked HTTP handler return."""
    async def scenario():
        first = await media.av_subscribe()
        second = await media.av_subscribe()
        media._signal_av_end()
        return first.get_nowait(), second.get_nowait()

    assert run(scenario()) == (None, None)
