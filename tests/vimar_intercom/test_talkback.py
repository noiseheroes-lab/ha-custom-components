"""Tests for talk-back: the µ-law encoder, the jitter buffer, the talker.

talkback.py imports no Home Assistant module. `start_talk` is exercised
against a stand-in with the four things of a websocket ActiveConnection
it uses, which is all the Home Assistant glue in talk_api.py passes it.
"""

import struct

import pytest

from custom_components.vimar_intercom import talkback as tb


def pcm(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def frame(value: int) -> bytes:
    """One 20 ms PCM frame holding a constant sample."""
    return pcm(*([value] * tb.FRAME_SAMPLES))


# ─── G.711 µ-law ─────────────────────────────────────────────────────

def test_ulaw_matches_the_reference_vectors():
    # CPython's own test_audioop vector for lin2ulaw at width 2, which
    # every audioop-based stack produced before 3.13 removed it.
    samples = (0, 0x1234, 0x4567, -0x4567, 0x7FFF, -0x8000, -1)
    assert tb.pcm16le_to_ulaw(pcm(*samples)) == b"\xff\xad\x8e\x0e\x80\x00\x7e"


def test_zero_is_ulaw_silence():
    assert tb.linear_to_ulaw(0) == 0xFF
    assert tb.pcm16le_to_ulaw(frame(0)) == tb.SILENCE_FRAME


def test_the_table_agrees_with_the_encoder_for_every_sample():
    everything = struct.pack("<65536h", *range(-32768, 32768))
    encoded = tb.pcm16le_to_ulaw(everything)
    assert len(encoded) == 65536
    assert all(encoded[i] == tb.linear_to_ulaw(s)
               for i, s in enumerate(range(-32768, 32768)))


def test_every_code_survives_decode_then_encode():
    # 0x7F is negative zero; it decodes to 0, whose code is 0xFF.
    for code in range(256):
        expected = 0xFF if code == 0x7F else code
        assert tb.linear_to_ulaw(tb.ulaw_to_linear(code)) == expected


def test_encoding_is_monotonic_in_amplitude():
    # Louder never encodes to a lower level: decode(encode(x)) rises with x.
    levels = [tb.ulaw_to_linear(tb.linear_to_ulaw(s))
              for s in range(-32768, 32768, 7)]
    assert levels == sorted(levels)


def test_a_trailing_odd_byte_is_ignored():
    assert tb.pcm16le_to_ulaw(pcm(0, 0x7FFF) + b"\x01") == b"\xff\x80"


# ─── the source the sender pulls from ────────────────────────────────

def test_nobody_talking_means_silence_forever():
    source = tb.TalkbackSource()
    assert {source.next_payload() for _ in range(50)} == {tb.SILENCE_FRAME}
    assert not source.talking


def test_voice_plays_after_the_prefill_then_silence_returns():
    source = tb.TalkbackSource()
    session = source.open()
    voice = tb.linear_to_ulaw(1000)
    session.push(frame(1000))
    session.push(frame(1000))
    # Two frames are below the prefill: nothing yet, so a network burst
    # does not turn into one-frame-on, one-frame-off chopping.
    assert source.next_payload() == tb.SILENCE_FRAME
    session.push(frame(1000))
    out = [source.next_payload() for _ in range(5)]
    assert out[:3] == [bytes([voice]) * tb.FRAME_SAMPLES] * 3
    assert out[3:] == [tb.SILENCE_FRAME] * 2


def test_running_dry_waits_for_the_prefill_again():
    source = tb.TalkbackSource()
    session = source.open()
    for _ in range(tb.PREFILL_FRAMES):
        session.push(frame(500))
    for _ in range(tb.PREFILL_FRAMES):
        assert source.next_payload() != tb.SILENCE_FRAME
    assert source.next_payload() == tb.SILENCE_FRAME
    session.push(frame(500))
    assert source.next_payload() == tb.SILENCE_FRAME


def test_the_buffer_keeps_the_newest_200_ms():
    source = tb.TalkbackSource()
    session = source.open()
    for value in range(1, 16):
        session.push(frame(value * 100))
    assert source.frames_dropped == 15 - tb.MAX_BUFFER_FRAMES
    first = source.next_payload()
    assert first[0] == tb.linear_to_ulaw(6 * 100)
    rest = [source.next_payload() for _ in range(tb.MAX_BUFFER_FRAMES - 1)]
    assert rest[-1][0] == tb.linear_to_ulaw(15 * 100)
    assert source.next_payload() == tb.SILENCE_FRAME


def test_frames_are_reassembled_from_any_chunking():
    source = tb.TalkbackSource()
    session = source.open()
    stream = b"".join(frame(v) for v in (100, 200, 300))
    for chunk in (stream[:7], stream[7:333], stream[333:901], stream[901:]):
        session.push(chunk)
    out = [source.next_payload()[0] for _ in range(3)]
    assert out == [tb.linear_to_ulaw(v) for v in (100, 200, 300)]


def test_an_oversized_message_keeps_its_newest_audio():
    source = tb.TalkbackSource()
    session = source.open()
    huge = b"".join(frame(v) for v in range(100, 100 + 40))
    session.push(huge + b"\x00")  # an odd tail too: stays sample aligned
    frames = [source.next_payload() for _ in range(tb.MAX_BUFFER_FRAMES)]
    assert frames[-1][0] == tb.linear_to_ulaw(139)
    assert all(len(f) == tb.FRAME_SAMPLES for f in frames)
    assert source.next_payload() == tb.SILENCE_FRAME


def test_the_newest_talker_takes_over_and_the_old_one_is_told():
    source = tb.TalkbackSource()
    ended = []
    first = source.open(ended.append)
    for _ in range(5):
        first.push(frame(1000))
    second = source.open()
    assert ended == [tb.END_REPLACED]
    assert not first.active and second.active
    # The first talker's queued words are dropped, and its late frames
    # are ignored rather than mixed in.
    first.push(frame(1000))
    assert source.next_payload() == tb.SILENCE_FRAME
    for _ in range(tb.PREFILL_FRAMES):
        second.push(frame(-2000))
    assert source.next_payload()[0] == tb.linear_to_ulaw(-2000)


def test_letting_go_plays_out_the_last_word_without_telling_anyone():
    source = tb.TalkbackSource()
    ended = []
    session = source.open(ended.append)
    for _ in range(4):
        session.push(frame(700))
    session.close()
    assert ended == []
    assert not source.talking
    session.push(frame(700))  # after the end: ignored
    out = [source.next_payload() for _ in range(6)]
    assert out.count(tb.SILENCE_FRAME) == 2


def test_the_end_of_the_call_ends_the_talker_and_drops_the_queue():
    source = tb.TalkbackSource()
    ended = []
    session = source.open(ended.append)
    for _ in range(5):
        session.push(frame(700))
    source.end_call()
    source.end_call()  # idempotent
    assert ended == [tb.END_CALL_ENDED]
    assert not session.active
    assert source.next_payload() == tb.SILENCE_FRAME


def test_a_failing_end_callback_does_not_escape():
    source = tb.TalkbackSource()

    def boom(_reason):
        raise ConnectionResetError

    source.open(boom)
    source.end_call()  # must not raise
    assert not source.talking


# ─── the websocket request ───────────────────────────────────────────

class FakeConnection:
    """What `start_talk` uses of Home Assistant's ActiveConnection."""

    def __init__(self, handler_limit=255):
        self.handlers = []
        self.subscriptions = {}
        self.results, self.events, self.errors = [], [], []
        self.limit = handler_limit

    def async_register_binary_handler(self, handler):
        if len(self.handlers) >= self.limit:
            raise RuntimeError("Too many binary handlers registered")
        self.handlers.append(handler)
        index = len(self.handlers) - 1

        def unregister():
            self.handlers[index] = None

        return index + 1, unregister

    def send_binary(self, handler_id, payload):
        # As websocket_api/http.py dispatches: first byte, then payload.
        handler = self.handlers[handler_id - 1]
        if handler is None:
            raise AssertionError("frame sent to an unregistered handler")
        handler(None, self, payload)

    def send_result(self, msg_id, result=None):
        self.results.append(msg_id)

    def send_event(self, msg_id, event):
        self.events.append((msg_id, event))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code))

    def close(self):
        # ActiveConnection.async_handle_close runs every subscription.
        for unsub in self.subscriptions.values():
            unsub()
        self.subscriptions.clear()


@pytest.mark.parametrize(("loaded", "in_call", "sending", "expected"), [
    (False, False, False, tb.REFUSE_NOT_LOADED),
    (True, False, True, tb.REFUSE_NO_CALL),
    (True, True, False, tb.REFUSE_NO_AUDIO),
    (True, True, True, None),
])
def test_talk_refusal(loaded, in_call, sending, expected):
    assert tb.talk_refusal(
        loaded=loaded, in_call=in_call, sending=sending) == expected


def test_a_refused_request_opens_nothing():
    source = tb.TalkbackSource()
    conn = FakeConnection()
    tb.start_talk(conn, 7, source, tb.REFUSE_NO_CALL)
    assert conn.errors == [(7, tb.REFUSE_NO_CALL)]
    assert conn.handlers == [] and conn.subscriptions == {}
    assert not source.talking


def test_a_talk_request_streams_until_the_socket_closes():
    source = tb.TalkbackSource()
    conn = FakeConnection()
    tb.start_talk(conn, 5, source, None)
    assert conn.results == [5]
    [(msg_id, start)] = conn.events
    assert msg_id == 5
    assert start == {"type": "start", "handler_id": 1,
                     "sample_rate": 8000, "frame_bytes": 320}
    for _ in range(tb.PREFILL_FRAMES):
        conn.send_binary(start["handler_id"], frame(1234))
    assert source.next_payload()[0] == tb.linear_to_ulaw(1234)

    conn.close()
    assert not source.talking
    assert conn.handlers == [None]
    # The browser stopped it, so no `end` event is sent into a socket
    # that is going away.
    assert [e for _, e in conn.events if e["type"] == "end"] == []


def test_the_call_ending_tells_the_browser_and_keeps_the_handler_quiet():
    source = tb.TalkbackSource()
    conn = FakeConnection()
    tb.start_talk(conn, 3, source, None)
    source.end_call()
    assert conn.events[-1] == (3, {"type": "end", "reason": tb.END_CALL_ENDED})
    # Frames still in flight reach a registered no-op, not an
    # unregistered handler Home Assistant would log an error for.
    conn.send_binary(1, frame(1000))
    assert source.next_payload() == tb.SILENCE_FRAME
    conn.subscriptions.pop(3)()  # the browser's unsubscribe
    assert conn.handlers == [None]


def test_a_second_browser_replaces_the_first():
    source = tb.TalkbackSource()
    tablet, phone = FakeConnection(), FakeConnection()
    tb.start_talk(tablet, 1, source, None)
    tb.start_talk(phone, 1, source, None)
    assert tablet.events[-1] == (1, {"type": "end", "reason": tb.END_REPLACED})
    for _ in range(tb.PREFILL_FRAMES):
        tablet.send_binary(1, frame(9000))
        phone.send_binary(1, frame(-9000))
    assert source.next_payload()[0] == tb.linear_to_ulaw(-9000)
    # The tablet closing later does not end the phone's session.
    tablet.close()
    assert source.talking


def test_no_free_handler_leaves_the_current_talker_alone():
    source = tb.TalkbackSource()
    tb.start_talk(FakeConnection(), 1, source, None)
    full = FakeConnection(handler_limit=0)
    tb.start_talk(full, 9, source, None)
    assert full.errors == [(9, "too_many")]
    assert source.talking
