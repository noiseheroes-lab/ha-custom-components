"""Tests for the H.264 consumer registry and SPS/PPS replay."""

from custom_components.vimar_intercom.media_handler import (
    ANNEX_B_START,
    MAX_CONSUMER_BACKLOG_BYTES,
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


def test_a_late_consumer_gets_the_cached_keyframe_first():
    """Primed with the parameter sets alone, a decoder attaching
    mid-stream has nothing to predict from: the next thing it receives
    is a P-slice referencing a picture it never got, and the pipeline
    remuxes rather than decodes, so it shows grey until the panel's next
    IDR — seconds, on a doorbell. The cached keyframe is SPS, PPS and
    the IDR they describe, already framed, so it starts on frame one."""
    registry = VideoStreamRegistry()
    first_sink, _ = collector()
    registry.add_consumer(first_sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
    registry.push_nal(SLICE)

    late_sink, late_received = collector()
    registry.add_consumer(late_sink)
    # One raw write of already-framed bytes, not three framed NALs.
    assert late_received == [b"".join(annex_b(SPS, PPS, IDR))]

    registry.push_nal(SLICE)
    assert late_received[-1] == ANNEX_B_START + SLICE


def test_a_late_consumer_with_no_keyframe_yet_still_gets_the_parameter_sets():
    """Between the parameter sets and the first IDR there is no keyframe
    to prime with, and the pair is still better than nothing."""
    registry = VideoStreamRegistry()
    registry.push_nal(SPS)
    registry.push_nal(PPS)

    late_sink, late_received = collector()
    assert registry.add_consumer(late_sink) is True
    assert late_received == annex_b(SPS, PPS)


def test_a_consumer_whose_sink_dies_while_priming_is_reported_as_dropped():
    """The caller holds its own reference to the consumer; without the
    answer it would keep feeding a pipeline the registry has dropped."""
    registry = VideoStreamRegistry()
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)

    def broken(_data: bytes) -> None:
        raise OSError("pipe closed")

    assert registry.add_consumer(broken) is False
    assert broken not in registry._consumers


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
    """A new SPS describes the pictures from the next IDR onwards, which
    is when the registry replays it and rebuilds the cached keyframe."""
    registry = VideoStreamRegistry()
    sink, received = collector()
    registry.add_consumer(sink)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)

    new_sps = bytes([0x67, 0x42, 0x80, 0x28])
    registry.push_nal(new_sps)
    assert registry.parameter_sets == (new_sps, PPS)

    received.clear()
    registry.push_nal(IDR)
    assert received == annex_b(new_sps, PPS, IDR)

    late_sink, late_received = collector()
    registry.add_consumer(late_sink)
    assert late_received == [b"".join(annex_b(new_sps, PPS, IDR))]


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

    # A slice alone proves nothing about consumer clearing on its own --
    # _started is also cleared by reset() and the slice-drop path checks
    # that independently. Push a full, valid SPS/PPS/IDR sequence too: if
    # reset() had not removed the consumer, this would reach it.
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)
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


def test_a_transiently_blocking_consumer_gets_everything_once_it_recovers():
    """A consumer that raises BlockingIOError must not be dropped, and
    must receive every backlogged byte, in order, once it recovers."""
    registry = VideoStreamRegistry()
    received = bytearray()
    blocking = {"on": True}

    def flaky(data: bytes) -> None:
        if blocking["on"]:
            err = BlockingIOError()
            err.characters_written = 0
            raise err
        received.extend(data)

    registry.add_consumer(flaky)
    registry.push_nal(IDR)   # held: no parameter sets cached yet
    registry.push_nal(SPS)   # still held: PPS missing
    registry.push_nal(PPS)   # flushes SPS, PPS, IDR -- all get backlogged
    assert bytes(received) == b""

    blocking["on"] = False
    registry.push_nal(SLICE)  # drains the backlog, then the new slice
    assert bytes(received) == b"".join(annex_b(SPS, PPS, IDR, SLICE))


def test_a_consumer_that_never_accepts_is_dropped_once_the_cap_is_exceeded():
    """The cap is on the accumulated backlog, not on the NAL in hand.

    Every slice pushed here is a quarter of the cap, so no single one
    could ever exceed it: only the backlog they pile up can, and the
    consumer must survive until it does and be dropped for good when it
    has. A cap applied to the NAL alone would keep this consumer
    forever, and with it an unbounded buffer."""
    registry = VideoStreamRegistry()
    calls = []

    def refuses(data: bytes) -> None:
        calls.append(len(data))
        err = BlockingIOError()
        err.characters_written = 0
        raise err

    registry.add_consumer(refuses)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)  # started; backlog still tiny, well under cap

    quarter = SLICE[:1] + bytes(MAX_CONSUMER_BACKLOG_BYTES // 4)
    assert len(quarter) < MAX_CONSUMER_BACKLOG_BYTES
    for _ in range(3):
        registry.push_nal(quarter)
        assert refuses in registry._consumers, (
            "dropped over a single NAL that fits the cap")
    # The fourth takes the accumulated backlog past the cap.
    registry.push_nal(quarter)
    assert refuses not in registry._consumers
    assert calls[-1] > MAX_CONSUMER_BACKLOG_BYTES

    calls_after_drop = len(calls)
    registry.push_nal(SLICE)  # a dropped consumer must not be called again
    assert len(calls) == calls_after_drop


def test_a_partial_write_is_completed_on_the_next_push():
    """write() accepting some bytes and not the rest must not corrupt or
    lose data -- the remainder is retried, in order, on the next push."""
    registry = VideoStreamRegistry()
    received = bytearray()
    first_call_done = {"yes": False}

    def half_writer(data: bytes) -> None:
        if not first_call_done["yes"]:
            first_call_done["yes"] = True
            n = 2
            received.extend(data[:n])
            err = BlockingIOError()
            err.characters_written = n
            raise err
        received.extend(data)

    registry.add_consumer(half_writer)
    registry.push_nal(SPS)
    registry.push_nal(PPS)
    registry.push_nal(IDR)

    assert bytes(received) == b"".join(annex_b(SPS, PPS, IDR))
