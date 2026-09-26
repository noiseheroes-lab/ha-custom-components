"""Tests for the card's talk-back block that need no browser.

The resampler is the one piece of the card's audio path whose output
can be checked without a microphone: it runs under Node exactly as the
card defines it (the AudioWorklet runs the same source text). Skipped
where Node is not installed; GitHub's Ubuntu runners have it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = (Path(__file__).resolve().parents[2] / "custom_components"
        / "vimar_intercom" / "frontend" / "vimar-intercom-card.js")
SOURCE = CARD.read_text(encoding="utf-8")
NODE = shutil.which("node")


def _resampler_source() -> str:
    start = SOURCE.index("class TalkResampler {")
    end = SOURCE.index("const TALK_WORKLET")
    return SOURCE[start:end]


def _run_resampler(in_rate: int, tone_hz: float, seconds: float) -> dict:
    script = _resampler_source() + f"""
const frames = [];
const r = new TalkResampler({in_rate}, (buf) => frames.push(buf));
const n = Math.round({in_rate} * {seconds});
// In render-quantum blocks of 128, as an AudioWorklet delivers them.
for (let off = 0; off < n; off += 128) {{
  const block = new Float32Array(Math.min(128, n - off));
  for (let i = 0; i < block.length; i++) {{
    block[i] = 0.5 * Math.sin(2 * Math.PI * {tone_hz} * (off + i) / {in_rate});
  }}
  r.process(block);
}}
const sizes = [...new Set(frames.map((b) => b.byteLength))];
// RMS of the second half, past the filter's settling.
const samples = [];
for (const b of frames) {{
  const v = new DataView(b);
  for (let i = 0; i < 160; i++) samples.push(v.getInt16(i * 2, true));
}}
const tail = samples.slice(samples.length / 2);
const rms = Math.sqrt(tail.reduce((a, s) => a + s * s, 0) / tail.length);
console.log(JSON.stringify({{ frames: frames.length, sizes, rms }}));
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True,
                         text=True, check=True, timeout=30)
    return json.loads(out.stdout)


# A 0.5 amplitude sine has an RMS of 0.354 full scale.
FULL_RMS = 0.5 / 2 ** 0.5 * 32767


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize("in_rate", [48000, 44100, 16000])
def test_speech_band_passes_as_20_ms_frames_at_8_khz(in_rate):
    result = _run_resampler(in_rate, 1000, 1.0)
    assert result["sizes"] == [320]
    assert result["frames"] in (49, 50)  # one second is 50 frames
    assert result["rms"] == pytest.approx(FULL_RMS, rel=0.05)


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_what_8_khz_cannot_carry_is_filtered_not_folded_back():
    # 6 kHz would alias to 2 kHz, right in the speech band.
    result = _run_resampler(48000, 6000, 1.0)
    assert result["rms"] < FULL_RMS * 0.05


def _string_table(lang: str) -> set[str]:
    block = SOURCE[SOURCE.index("const TALK_STRINGS = {"):]
    body = block[block.index(f"  {lang}: {{"):]
    body = body[:body.index("\n  },")]
    return set(re.findall(r"^\s{4}(\w+):", body, re.MULTILINE))


def test_talk_strings_exist_in_english_and_italian():
    english, italian = _string_table("en"), _string_table("it")
    assert english and english == italian
    for key in ("talk", "talk_needs_https", "talk_denied", "talk_replaced"):
        assert key in english


def test_the_card_uses_the_command_the_integration_registers():
    from custom_components.vimar_intercom.const import DOMAIN

    assert "const TALK_COMMAND = `${DOMAIN}/talk`;" in SOURCE
    assert f'const DOMAIN = "{DOMAIN}";' in SOURCE


def test_the_microphone_is_asked_for_with_voice_processing_on():
    for option in ("echoCancellation: true", "noiseSuppression: true",
                   "autoGainControl: true"):
        assert option in SOURCE
