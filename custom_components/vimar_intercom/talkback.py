"""Talk-back: the browser's microphone, played out of the entrance panel.

During a call the media layer sends the panel one 20 ms PCMU packet
every 20 ms, because a call that carries nothing from this side is
ended by the panel after about ten seconds. This module decides what
goes into each of those packets: the voice of whoever is holding the
card's Talk button when there is some, silence otherwise. The sender
pulls; nothing here owns a timer or a socket.

The browser sends 16-bit little-endian mono PCM at 8 kHz in 20 ms
frames (320 bytes), already resampled, over a Home Assistant websocket
binary handler (`talk_api.py`). It is converted to G.711 µ-law here, in
pure Python: `audioop` was removed in Python 3.13, and one lookup per
sample is cheap at 8000 samples a second.

No Home Assistant import, so all of it is unit tested; `start_talk`
works against anything shaped like a websocket `ActiveConnection`.
"""

from __future__ import annotations

import logging
import sys
from array import array
from collections import deque
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20 ms at 8 kHz, the packet size the panel is sent
PCM_FRAME_BYTES = FRAME_SAMPLES * 2
# G.711 µ-law code for zero amplitude.
SILENCE_FRAME = b"\xff" * FRAME_SAMPLES

# At most 200 ms of voice waits to be sent. A browser that sends faster
# than the 20 ms clock drains — clock drift, or a burst after a network
# stall — loses its oldest audio instead of building up a delay that
# never goes away, which is what makes an intercom unusable.
MAX_BUFFER_FRAMES = 10
# Playout starts once 60 ms are queued, and starts over after running
# dry. Frames arrive in bursts over the network; starting on the first
# one would play one frame, run dry, play one frame, and chop every
# word with gaps of silence.
PREFILL_FRAMES = 3
# The most audio one binary message can get converted. More than the
# buffer holds would be dropped anyway; skipping it first bounds the
# work one message can cause.
MAX_MESSAGE_BYTES = MAX_BUFFER_FRAMES * PCM_FRAME_BYTES

# Why a talk session ended, as the card is told.
END_REPLACED = "replaced"
END_CALL_ENDED = "call_ended"
END_STOPPED = "stopped"

# Why a talk request is refused: websocket error code → message.
REFUSE_NOT_LOADED = "not_loaded"
REFUSE_NO_CALL = "no_call"
REFUSE_NO_AUDIO = "no_audio"
REFUSALS = {
    REFUSE_NOT_LOADED: "The intercom is not loaded",
    REFUSE_NO_CALL: "There is no call in progress",
    REFUSE_NO_AUDIO: "The call carries no audio from Home Assistant",
    "too_many": "Too many streams are open on this connection",
}


# ─── G.711 µ-law ────────────────────────────────────────────────────

_ULAW_BIAS = 0x84 >> 2  # the reference encoder works on 14-bit magnitudes
_ULAW_CLIP = 8159
_SEGMENT_ENDS = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def linear_to_ulaw(sample: int) -> int:
    """Encode one signed 16-bit sample as a G.711 µ-law byte.

    The reference algorithm (Sun's g711.c, the one `audioop.lin2ulaw`
    used): the top 14 bits, sign and magnitude, a bias so that the
    segments are powers of two, then a 3-bit segment and 4-bit mantissa,
    all inverted.
    """
    value = sample >> 2
    if value < 0:
        value, mask = -value, 0x7F
    else:
        mask = 0xFF
    value = min(value, _ULAW_CLIP) + _ULAW_BIAS
    for segment, end in enumerate(_SEGMENT_ENDS):
        if value <= end:
            return ((segment << 4) | ((value >> (segment + 1)) & 0x0F)) ^ mask
    return 0x7F ^ mask


def ulaw_to_linear(code: int) -> int:
    """Decode one µ-law byte to a signed 16-bit sample (the inverse)."""
    code = ~code & 0xFF
    magnitude = (((code & 0x0F) << 3) + 0x84) << ((code & 0x70) >> 4)
    return 0x84 - magnitude if code & 0x80 else magnitude - 0x84


# Indexed by the top 14 bits of the sample as an unsigned 16-bit number,
# which is what `linear_to_ulaw` looks at: 16 KB, built in a few ms.
_ULAW_TABLE = bytes(
    linear_to_ulaw((index << 2) - 65536 if index >= 0x2000 else index << 2)
    for index in range(0x4000))


def pcm16le_to_ulaw(pcm: bytes) -> bytes:
    """Convert 16-bit little-endian mono PCM to µ-law, one byte per sample.

    A trailing odd byte is ignored; callers keep whole samples.
    """
    samples = array("H")
    samples.frombytes(pcm[: len(pcm) & ~1])
    if sys.byteorder == "big":
        samples.byteswap()
    table = _ULAW_TABLE
    return bytes([table[value >> 2] for value in samples])


# ─── the audio source the 20 ms sender pulls from ───────────────────

class TalkSession:
    """One browser's claim on the panel's speaker.

    Obtained from `TalkbackSource.open`. Only the newest session is
    heard; an older one that is still pushing is ignored rather than
    mixed in, because two people talking at once through one small
    speaker is noise, not a feature.
    """

    def __init__(self, source: TalkbackSource,
                 on_end: Callable[[str], None] | None) -> None:
        """Bind the session to its source; `on_end` hears a remote end."""
        self._source = source
        self._on_end = on_end
        self._closed = False

    @property
    def active(self) -> bool:
        """True while this session's audio is the one being played."""
        return not self._closed and self._source._session is self

    def push(self, pcm: bytes) -> None:
        """Queue PCM from the browser. Ignored once the session is over."""
        if self.active:
            self._source._feed(pcm)

    def close(self, reason: str = END_STOPPED, *, notify: bool = False) -> None:
        """End the session; idempotent.

        `notify` tells the browser, through `on_end`. It is left off when
        the browser is the one that stopped: it already knows, and may no
        longer be there to be told.
        """
        if self._closed:
            return
        self._closed = True
        self._source._release(self, reason)
        if notify and self._on_end is not None:
            try:
                self._on_end(reason)
            except Exception:  # noqa: BLE001 - a dead socket must not break the call
                _LOGGER.debug("Could not report the end of talk-back",
                              exc_info=True)


class TalkbackSource:
    """What the panel hears: queued talk-back frames, else silence.

    `next_payload` is called by the media sender once per 20 ms tick and
    always returns one 160-byte µ-law frame, so the packet rate the
    panel relies on never depends on the browser.
    """

    def __init__(self) -> None:
        """Start silent, with nobody talking."""
        self._frames: deque[bytes] = deque(maxlen=MAX_BUFFER_FRAMES)
        self._pending = b""
        self._playing = False
        self._session: TalkSession | None = None
        # Counted, never logged per frame: see the logging promise in
        # ARCHITECTURE.md's threat model.
        self.frames_in = 0
        self.frames_dropped = 0

    @property
    def talking(self) -> bool:
        """True while a browser holds the speaker."""
        return self._session is not None

    def open(self, on_end: Callable[[str], None] | None = None) -> TalkSession:
        """Give the speaker to a new talker, taking it from any current one.

        The newest wins: the person pressing Talk now is the one in front
        of the screen, and a tab left talking in another room must not
        lock everyone else out until it is found.
        """
        if self._session is not None:
            self._session.close(END_REPLACED, notify=True)
        self._clear()
        session = TalkSession(self, on_end)
        self._session = session
        _LOGGER.debug("Talk-back started")
        return session

    def end_call(self) -> None:
        """The call is over: end any session and drop what was queued."""
        if self._session is not None:
            self._session.close(END_CALL_ENDED, notify=True)
        self._clear()

    def next_payload(self) -> bytes:
        """The next 20 ms for the panel: voice when there is some."""
        if not self._playing:
            if len(self._frames) < PREFILL_FRAMES:
                return SILENCE_FRAME
            self._playing = True
        if not self._frames:
            self._playing = False
            return SILENCE_FRAME
        return self._frames.popleft()

    def stats(self) -> str:
        """One line for the per-call media summary."""
        return (f"talk-back frames in={self.frames_in} "
                f"dropped={self.frames_dropped}")

    def reset_stats(self) -> None:
        """Zero the counters, at the start of a call."""
        self.frames_in = 0
        self.frames_dropped = 0

    # ── internals, used by TalkSession ──

    def _feed(self, pcm: bytes) -> None:
        data = self._pending + pcm
        whole = len(data) - len(data) % PCM_FRAME_BYTES
        self._pending = data[whole:]
        # Frames the buffer could not hold anyway are skipped before
        # they are converted: the newest ones are kept.
        first = max(0, whole - MAX_MESSAGE_BYTES)
        self.frames_in += first // PCM_FRAME_BYTES
        self.frames_dropped += first // PCM_FRAME_BYTES
        for start in range(first, whole, PCM_FRAME_BYTES):
            if len(self._frames) == MAX_BUFFER_FRAMES:
                self.frames_dropped += 1
            self._frames.append(
                pcm16le_to_ulaw(data[start:start + PCM_FRAME_BYTES]))
            self.frames_in += 1

    def _release(self, session: TalkSession, reason: str) -> None:
        if self._session is not session:
            return
        self._session = None
        self._pending = b""
        _LOGGER.debug("Talk-back ended: %s", reason)
        # A talker who lets go keeps the last word: what is queued plays
        # out. The other ends drop it — `open` and `end_call` clear.

    def _clear(self) -> None:
        self._frames.clear()
        self._pending = b""
        self._playing = False


# ─── the websocket request, against a duck-typed connection ─────────

def talk_refusal(*, loaded: bool, in_call: bool, sending: bool) -> str | None:
    """Why a talk request cannot be served now, or None if it can.

    `sending` is whether the media layer is sending the panel audio at
    all: with no SRTP key offered there is no stream to put a voice in.
    """
    if not loaded:
        return REFUSE_NOT_LOADED
    if not in_call:
        return REFUSE_NO_CALL
    if not sending:
        return REFUSE_NO_AUDIO
    return None


def start_talk(connection: Any, msg_id: int, source: TalkbackSource,
               refusal: str | None) -> None:
    """Serve one `vimar_intercom/talk` subscription.

    The reply is a subscription, as Assist's audio pipeline does it:
    a result, then a `start` event carrying the binary handler ID the
    browser prefixes to every audio frame, and an `end` event when the
    call ends or another talker takes over. The frontend's
    `subscribeMessage` passes events on but not the result's payload,
    which is why the ID travels in an event.

    The binary handler stays registered, as a no-op once the session is
    over, until the browser unsubscribes or the websocket closes; both
    run the subscription's cleanup. Unregistering it earlier would make
    Home Assistant log an error for every frame still in flight.
    """
    if refusal is not None:
        connection.send_error(msg_id, refusal, REFUSALS[refusal])
        return

    holder: list[TalkSession] = []

    def on_audio(_hass: Any, _connection: Any, data: bytes) -> None:
        if holder:
            holder[0].push(data)

    try:
        handler_id, unregister = connection.async_register_binary_handler(
            on_audio)
    except RuntimeError:
        # Registered before the session opens, so this failure cannot
        # silence whoever is talking now.
        connection.send_error(msg_id, "too_many", REFUSALS["too_many"])
        return

    def on_end(reason: str) -> None:
        connection.send_event(msg_id, {"type": "end", "reason": reason})

    session = source.open(on_end)
    holder.append(session)

    def unsubscribe() -> None:
        session.close(END_STOPPED)
        unregister()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    connection.send_event(msg_id, {
        "type": "start",
        "handler_id": handler_id,
        "sample_rate": SAMPLE_RATE,
        "frame_bytes": PCM_FRAME_BYTES,
    })
