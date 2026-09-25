"""Tests for the H.264 consumer registry and SPS/PPS replay."""

from custom_components.vimar_intercom.media_handler import (
    ANNEX_B_START,
    VideoStreamRegistry,
)

SPS = bytes([0x67, 0x42, 0x80, 0x1F])
PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
IDR = bytes([0x65, 0x11, 0x22, 0x33])
SLICE = bytes([0x41, 0x44, 0x55, 0x66])


def collector():
    """Return a (sink, received) pair."""
    received: list[bytes] = []
    return received.append, received


def annex_b(*nals: bytes) -> list[bytes]:
    """Expected framing for the given NAL units."""
    return [ANNEX_B_START + nal for nal in nals]


def test_consumer_receives_nals_in_annex_b_framing():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    assert received == annex_b(SPS, PPS, IDR)


def test_slices_before_the_first_idr_are_dropped():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SLICE)
    assert received == []


def test_idr_before_parameter_sets_is_held_until_they_arrive():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(IDR)
    assert received == []
    registry.push_nal(SPS)
    assert received == []
    registry.push_nal(PPS)
    assert received == annex_b(SPS, PPS, IDR)


def test_a_late_consumer_gets_the_cached_parameter_sets_first():
    registry = VideoStreamRegistry()
    first_sink, _ = collector()
    registry.add_consumer(first_sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.push_nal(SLICE)

    late_sink, late_received = collector()
    registry.add_consumer(late_sink)
    assert late_received == annex_b(SPS, PPS)

    registry.push_nal(SLICE)
    assert late_received == annex_b(SPS, PPS, SLICE)


def test_a_late_consumer_with_no_cached_parameter_sets_gets_nothing():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    assert received == []


def test_parameter_sets_are_replayed_before_every_idr():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    received.clear()
    registry.push_nal(IDR)
    assert received == annex_b(SPS, PPS, IDR)


def test_updated_parameter_sets_replace_the_cache():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)

    new_sps = bytes([0x67, 0x42, 0x80, 0x28])
    registry.push_nal(new_sps)
    late_sink, late_received = collector()
    registry.add_consumer(late_sink)
    assert late_received == annex_b(new_sps, PPS)


def test_removed_consumers_stop_receiving():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.remove_consumer(sink)
    received.clear()
    registry.push_nal(SLICE)
    assert received == []


def test_a_failing_consumer_is_dropped_without_affecting_the_others():
    registry = VideoStreamRegistry()

    def broken(_data: bytes) -> None:
        raise OSError("pipe closed")

    good_sink, good_received = collector()
    registry.add_consumer(broken)
    registry.add_consumer(good_sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    assert good_received == annex_b(SPS, PPS, IDR)
    registry.push_nal(SLICE)
    assert good_received == annex_b(SPS, PPS, IDR, SLICE)


def test_reset_clears_the_cache_and_the_consumers():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.reset()
    assert registry.parameter_sets is None
    received.clear()
    registry.push_nal(SLICE)
    assert received == []


def test_parameter_sets_property_reports_the_cached_pair():
    registry = VideoStreamRegistry()
    assert registry.parameter_sets is None
    registry.push_nal(SPS)
    assert registry.parameter_sets is None
    registry.push_nal(PPS)
    assert registry.parameter_sets == (SPS, PPS)


def test_empty_nals_are_ignored():
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(b"")
    assert received == []
