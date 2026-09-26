"""Synthetic phone photos of the indoor unit's screen, through read_qr_image.

The owner's first real photo of the Tab 5S's QR was not read at all: the
decoder made one zbar pass over the raw 12 MP frame. These tests pin the
multi-attempt search that replaced it, on pictures made by screen_photo.py
from fake credentials.

The quick tests run with the suite. The benchmark behind them runs only
when asked, because it takes minutes:

    VIMAR_QR_BENCHMARK=300 python -m pytest -s tests/vimar_intercom/test_qr_photo.py

It prints the success rate of the old single raw pass and of the new
search, per kind of photo, and the median and worst decode time.
VIMAR_QR_BENCHMARK_SEED picks the first seed, to shard a long run.
"""

import io
import os
import statistics
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PIL.Image")
pytest.importorskip("numpy")
pytest.importorskip("qrcode")
try:
    # Not importorskip: see test_qr_image.py. A missing libzbar is an
    # ImportError importorskip no longer skips on, an unloadable one an
    # OSError it never catches.
    from pyzbar import pyzbar
except (ImportError, OSError) as err:
    pytest.skip(f"libzbar cannot be loaded: {err}", allow_module_level=True)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from custom_components.vimar_intercom import qr  # noqa: E402

from screen_photo import realistic_payload, screen_photo  # noqa: E402

# Training seeds for choosing the ladder were 1000-3039; the benchmark and
# the quick cases below are drawn from seeds it never saw.
BENCHMARK_FIRST_SEED = 100_000


def photo(seed: int, difficulty: float = 1.0, light_on_dark: bool = False):
    """The payload and the JPEG bytes of one synthetic photo."""
    rng = np.random.default_rng(seed)
    payload = realistic_payload(rng)
    data, params = screen_photo(payload, rng, difficulty, light_on_dark)
    return payload, data, params


def single_raw_pass(data: bytes) -> set[bytes]:
    """What read_qr_image did before: one zbar call on the decoded image."""
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        symbols = pyzbar.decode(image, symbols=[pyzbar.ZBarSymbol.QRCODE])
    return {symbol.data for symbol in symbols}


# Held-out photos the single raw pass cannot read, one per way the new
# search gets there: a rescaled copy, the straightened code, and the
# inverted threshold for a light-on-dark screen.
QUICK_CASES = [
    pytest.param(100_000, 1.0, False, id="rescaled"),
    pytest.param(100_001, 1.0, False, id="rectified"),
    pytest.param(100_009, 1.0, True, id="light-on-dark"),
]


@pytest.mark.parametrize(("seed", "difficulty", "light_on_dark"), QUICK_CASES)
def test_a_phone_photo_of_the_screen_is_read(seed, difficulty, light_on_dark):
    payload, data, params = photo(seed, difficulty, light_on_dark)
    assert payload.encode() not in single_raw_pass(data), (
        "this fixture no longer defeats the old decoder; pick a harder seed")
    assert qr.read_qr_image(data) == payload, params
    assert qr.decode_qr(payload)["ID"] == "60901"


def test_the_exif_orientation_is_applied_before_anything_else():
    # Portrait: the sensor's landscape pixels, stored with Orientation 6.
    stored = Image.new("L", (40, 30), 255)
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    stored.save(buffer, format="JPEG", exif=exif.tobytes())
    with Image.open(io.BytesIO(buffer.getvalue())) as image:
        assert qr._load_grayscale(image).size == (30, 40)


def _count_decodes(monkeypatch) -> list[int]:
    calls = []

    def decode(image, symbols=None):
        calls.append(1)
        return []

    monkeypatch.setattr(pyzbar, "decode", decode)
    return calls


def test_the_search_is_capped_in_attempts(monkeypatch):
    calls = _count_decodes(monkeypatch)
    monkeypatch.setattr(qr, "_MAX_SCAN_ATTEMPTS", 3)
    _, data, _ = photo(1)
    with pytest.raises(qr.QRNotFoundError):
        qr.read_qr_image(data)
    assert len(calls) == 3


def test_the_search_is_capped_in_time(monkeypatch):
    calls = _count_decodes(monkeypatch)
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(qr, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    _, data, _ = photo(1)
    with pytest.raises(qr.QRNotFoundError):
        qr.read_qr_image(data)
    # Ten seconds pass between reading the clock at the start and after
    # the first attempt, well past the budget.
    assert len(calls) == 1


def test_the_whole_ladder_runs_when_nothing_is_found(monkeypatch):
    calls = _count_decodes(monkeypatch)
    _, data, _ = photo(1)
    with pytest.raises(qr.QRNotFoundError):
        qr.read_qr_image(data)
    assert len(calls) == min(len(qr._SCAN_LADDER), qr._MAX_SCAN_ATTEMPTS)


def test_a_tiny_image_is_enlarged_before_it_is_read(monkeypatch):
    sizes = []

    def decode(image, symbols=None):
        sizes.append(max(image.size))
        return []

    monkeypatch.setattr(pyzbar, "decode", decode)
    buffer = io.BytesIO()
    Image.new("L", (200, 150), 255).save(buffer, format="PNG")
    with pytest.raises(qr.QRNotFoundError):
        qr.read_qr_image(buffer.getvalue())
    assert sizes[0] == 200
    assert qr._MIN_LONGEST_SIDE in sizes


# The benchmark mix: mostly careless-but-honest photos, some worse ones,
# and a few of a screen drawing the code light on dark.
_KINDS = (
    ("ordinary", 1.0, False, 7),
    ("hard", 1.5, False, 2),
    ("light-on-dark", 1.0, True, 1),
)


def _kind(seed: int):
    slot = seed % sum(weight for *_, weight in _KINDS)
    for name, difficulty, light_on_dark, weight in _KINDS:
        if slot < weight:
            return name, difficulty, light_on_dark
        slot -= weight
    raise AssertionError


@pytest.mark.skipif(not os.environ.get("VIMAR_QR_BENCHMARK"),
                    reason="set VIMAR_QR_BENCHMARK=<count> to run the benchmark")
def test_benchmark_old_single_pass_against_the_new_search():
    count = int(os.environ["VIMAR_QR_BENCHMARK"])
    first = int(os.environ.get("VIMAR_QR_BENCHMARK_SEED", BENCHMARK_FIRST_SEED))
    results = {name: {"old": 0, "new": 0, "n": 0, "times": [], "old_times": []}
               for name, *_ in _KINDS}
    regressions = []
    failures = []
    for seed in range(first, first + count):
        name, difficulty, light_on_dark = _kind(seed)
        payload, data, params = photo(seed, difficulty, light_on_dark)
        row = results[name]
        row["n"] += 1

        start = time.perf_counter()
        old = payload.encode() in single_raw_pass(data)
        row["old_times"].append(time.perf_counter() - start)

        start = time.perf_counter()
        try:
            new = qr.read_qr_image(data) == payload
        except qr.QRNotFoundError:
            new = False
        row["times"].append(time.perf_counter() - start)

        row["old"] += old
        row["new"] += new
        if old and not new:
            regressions.append(seed)
        if not new:
            failures.append((seed, name, params))

    print(f"\nseeds {first}..{first + count - 1}")
    print(f"{'kind':15s} {'n':>4s} {'old':>7s} {'new':>7s} "
          f"{'old med':>8s} {'new med':>8s} {'new p95':>8s} {'new max':>8s}")
    everything = {"old": 0, "new": 0, "n": 0, "times": [], "old_times": []}
    for name, row in [*results.items(), ("all", everything)]:
        if name != "all":
            for key in everything:
                everything[key] += row[key]
        if not row["n"]:
            continue
        times = sorted(row["times"])
        print(f"{name:15s} {row['n']:4d} {row['old'] / row['n']:7.1%} "
              f"{row['new'] / row['n']:7.1%} "
              f"{statistics.median(row['old_times']) * 1000:6.0f}ms "
              f"{statistics.median(times) * 1000:6.0f}ms "
              f"{times[int(0.95 * (len(times) - 1))] * 1000:6.0f}ms "
              f"{times[-1] * 1000:6.0f}ms")
    for seed, name, params in failures:
        print(f"  not read: seed {seed} ({name}) {params}")

    assert not regressions, f"read by the old pass but not the new: {regressions}"
    assert everything["new"] >= everything["old"]
