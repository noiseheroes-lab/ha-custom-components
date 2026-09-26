"""Read and decode the QR code a Vimar indoor unit generates per user.

The Vimar View app scans the same code to pair a phone.

The QR encodes base64 of `key[32] || ciphertext || iv[16]`. The ciphertext
is AES-256-CBC with PKCS#7 padding, using the embedded key and IV. The
plaintext is `KEY=VALUE` pairs separated by newlines or `&`.

Reverse engineered from `com.vimar.vmsipsdk.utility.QrUtil`.

Nothing in this module logs. The decrypted payload contains the SIP
password and must never reach the log or an exception message. The same
goes for a QR image: it is the credentials, just not yet decoded.
"""

from __future__ import annotations

import base64
import binascii
import io
import time
from urllib.parse import unquote

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_LENGTH = 32
IV_LENGTH = 16
BLOCK_SIZE_BITS = 128
MIN_PAYLOAD_LENGTH = KEY_LENGTH + IV_LENGTH + 16
# A real payload is a few hundred bytes. The ceiling is here because
# this runs on the event loop during the config flow: base64-decoding
# and AES-decrypting a multi-megabyte paste would stall Home Assistant
# for as long as it took.
MAX_PAYLOAD_LENGTH = 8 * 1024

REQUIRED_FIELDS: tuple[str, ...] = ("ID", "PWD", "CDOMAIN")

# A phone screenshot is well under 10 MB and a 48 MP photo about 20 MB as
# JPEG. file_upload itself accepts 100 MB, and all of it would be read
# into memory and handed to the decoder.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
# Checked from the header, before any pixel is decoded: a small file can
# still declare an enormous canvas. 64 MP leaves room for a 48 MP photo.
MAX_IMAGE_PIXELS = 64_000_000

# ISO base media brands of HEIF images carrying HEVC, which is what an
# iPhone photo is. Pillow opens none of them without a plugin that Home
# Assistant does not ship.
_HEIC_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
})


class QRDecodeError(ValueError):
    """The QR payload could not be decoded into usable credentials.

    Messages are deliberately generic: they must be safe to show in the
    config flow and to write to the log.
    """


def decrypt_payload(payload: str) -> str:
    """Return the plaintext of a base64 Vimar QR payload.

    Raises QRDecodeError if the payload is not valid base64, is too
    short or too long, or does not decrypt to valid UTF-8.
    """
    payload = payload.strip()
    if len(payload) > MAX_PAYLOAD_LENGTH:
        raise QRDecodeError("payload is too long to be a Vimar QR code")

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as err:
        raise QRDecodeError("payload is not valid base64") from err

    if len(raw) < MIN_PAYLOAD_LENGTH:
        raise QRDecodeError("payload is too short to be a Vimar QR code")

    key = raw[:KEY_LENGTH]
    iv = raw[-IV_LENGTH:]
    ciphertext = raw[KEY_LENGTH:-IV_LENGTH]

    if not ciphertext or len(ciphertext) % 16:
        raise QRDecodeError("payload has an unexpected length")

    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(BLOCK_SIZE_BITS).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as err:
        raise QRDecodeError("payload could not be decrypted") from err

    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as err:
        raise QRDecodeError("payload could not be decrypted") from err


def parse_fields(plaintext: str) -> dict[str, str]:
    """Parse `KEY=VALUE` pairs separated by newlines or `&`."""
    separator = "\n" if "\n" in plaintext else "&"
    fields: dict[str, str] = {}
    for pair in plaintext.strip().split(separator):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        fields[key.strip()] = unquote(value.strip())
    return fields


def decode_qr(payload: str) -> dict[str, str]:
    """Decrypt and parse a QR payload, checking the mandatory fields."""
    fields = parse_fields(decrypt_payload(payload))

    missing = [name for name in REQUIRED_FIELDS if not fields.get(name)]
    if missing:
        raise QRDecodeError(
            "QR code is missing required fields: " + ", ".join(missing))

    return fields


class QRInputError(Exception):
    """The form did not yield a QR payload to decode.

    Not a ValueError, so that nothing catching QRDecodeError's base can
    swallow one and report the wrong problem. `error_key` is the
    config flow's translation key for the message the user sees.
    """

    error_key = "invalid_qr"


class QRInputMissingError(QRInputError):
    """Neither an image nor pasted text was given."""

    error_key = "no_qr_input"


class QRImageTooLargeError(QRInputError):
    """The upload is too big to be a photo or screenshot of a QR code."""

    error_key = "image_too_large"


class QRImageHEICError(QRInputError):
    """The upload is a HEIC photo, which Pillow cannot open."""

    error_key = "image_heic"


class QRImageUnreadableError(QRInputError):
    """The upload is not an image Pillow can decode."""

    error_key = "image_unreadable"


class QRNotFoundError(QRInputError):
    """The image was read but no QR code was found in it."""

    error_key = "qr_not_found"


class QRAmbiguousError(QRInputError):
    """The image holds more than one different QR code."""

    error_key = "multiple_qr"


class QRReaderUnavailableError(QRInputError):
    """pyzbar or its native libzbar cannot be loaded on this host."""

    error_key = "qr_reader_unavailable"


def choose_qr_input(image_id: str | None, pasted: str | None) -> tuple[str, str]:
    """Pick which of the two form fields to read: ("image", id) or ("text", payload).

    The image wins when both are filled. The text field is redisplayed
    with whatever was pasted last, so a stale paste is the likelier of
    the two to be the mistake.
    """
    if image_id:
        return "image", image_id
    if pasted and pasted.strip():
        return "text", pasted
    raise QRInputMissingError


def is_heic(data: bytes) -> bool:
    """Whether the bytes are a HEIF/HEIC image, from the `ftyp` box."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(data[0:4], "big")
    if box_size < 16:
        return False
    # Major brand at 8, minor version at 12, compatible brands from 16.
    brands = [data[8:12]] + [
        data[i:i + 4] for i in range(16, min(box_size, len(data)) - 3, 4)]
    return any(brand in _HEIC_BRANDS for brand in brands)


def read_qr_image(data: bytes) -> str:
    """Return the text of the single QR code in an image.

    Blocking and CPU-bound: run it in the executor. pyzbar, Pillow and
    numpy are imported here, not at module level, because a Home
    Assistant Core install without the native libzbar would otherwise
    fail to import the whole integration, text paste included.

    A screenshot decodes on the first attempt. A phone photo of the
    indoor unit's screen usually does not: see `_scan` for what is tried
    and how long it may take.

    Several identical QR codes (or zbar reporting one twice) count as
    one. Several different ones are refused rather than guessed at: the
    wrong one could belong to another user of the same panel.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise QRImageTooLargeError
    if is_heic(data):
        raise QRImageHEICError
    deadline = time.monotonic() + _SCAN_BUDGET_SECONDS

    try:
        import numpy  # noqa: F401, PLC0415 - see the docstring
        from PIL import Image  # noqa: PLC0415 - see the docstring
        from pyzbar import pyzbar  # noqa: PLC0415 - see the docstring
    except (ImportError, OSError) as err:
        # pyzbar loads libzbar through ctypes at import: a missing
        # library is an ImportError, an unloadable one an OSError.
        raise QRReaderUnavailableError from err

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise QRImageTooLargeError
            gray = _load_grayscale(image)
        found = _scan(gray, pyzbar, deadline)
    except QRImageTooLargeError:
        raise
    except Image.DecompressionBombError as err:
        raise QRImageTooLargeError from err
    except (OSError, SyntaxError, ValueError, pyzbar.PyZbarError) as err:
        raise QRImageUnreadableError from err

    if not found:
        raise QRNotFoundError
    if len(found) > 1:
        raise QRAmbiguousError
    # Undecodable bytes become U+FFFD, which is not base64, so a QR that
    # is readable but not Vimar's fails in decode_qr like a bad paste.
    return found.pop().decode("utf-8", errors="replace")


# How a photo is searched for a QR code.
#
# zbar reads a clean screenshot at any size, but a photo of the indoor
# unit's backlit LCD is close to the worst case for it: moire from the
# subpixel grid, glare, noise, perspective, and a dense code (the whole
# encrypted payload, around version 13 to 17) filling a corner of a
# 12-megapixel frame. A phone app copes by trying frame after frame; this
# tries one image many ways instead, cheapest and most often successful
# first, and stops at the first attempt that finds anything.
#
# The order and the set come from a benchmark of synthetic screen photos
# (tests/vimar_intercom/test_qr_photo.py), not from intuition. Global
# thresholds (Otsu), histogram equalisation, autocontrast and median
# filtering were measured and dropped: none decoded anything the others
# missed, and Otsu and equalisation did worse than no processing at all.
#
# Each entry is (source, transform). A source is the longest side, in
# pixels, the grayscale image is rescaled to (0: as uploaded), or
# _RECTIFIED for the QR region straightened out of its perspective.
#
# The image as uploaded, at full size, comes first: it is what the single
# pass this replaced read, so a screenshot, or two different codes side by
# side, still decode on the first attempt as before. "smoothed" blurs away
# moire before thresholding, which pays only where a module spans many
# pixels: the straightened code and the larger copies.
_RECTIFIED = -1
_SCAN_LADDER: tuple[tuple[int, str], ...] = (
    (0, "plain"),
    (800, "adaptive"),
    (_RECTIFIED, "smoothed"),
    (1500, "adaptive"),
    (2000, "smoothed"),
    (_RECTIFIED, "adaptive"),
    (1100, "adaptive"),
    (800, "inverted"),
    (1100, "inverted"),
    (_RECTIFIED, "inverted"),
    (1500, "inverted"),
)
# Bounds on the whole search, image decoding included. On the Apple M4
# the benchmark ran on, a 12 MP photo decodes in about 0.1 s and the full
# ladder, when nothing is found, takes 0.2 s; 12 MP of pure noise, the
# slowest input tried, 0.5 s. The reference machine is a fanless two-core
# 7 W box, five to ten times slower: the clock is what bounds that worst
# case there, and it overruns by at most one attempt.
_SCAN_BUDGET_SECONDS = 3.0
_MAX_SCAN_ATTEMPTS = 12

# zbar needs a few pixels per module; below this the image is enlarged.
_MIN_LONGEST_SIDE = 800
# Window of the adaptive threshold. Wide enough to span several modules at
# the working scales, narrow enough to follow a glare gradient.
_ADAPTIVE_WINDOW = 31
_ADAPTIVE_OFFSET = 5
# Where the QR region is looked for, and how finely.
_LOCATE_LONGEST_SIDE = 1000
_LOCATE_BLOCK = 8
# The straightened code is drawn this size, with this much margin added
# around the located region so the quiet zone comes along with it.
_RECTIFIED_SIDE = 700
_RECTIFIED_MARGIN = 0.12


def _load_grayscale(image):
    """Decode the image upright, as one 8-bit luminance plane."""
    from PIL import ImageOps  # noqa: PLC0415 - see read_qr_image

    if image.format == "JPEG":
        # libjpeg then skips chroma upsampling and colour conversion,
        # roughly halving decode time for a 12 MP photo.
        image.draft("L", image.size)
    # zbar reads a rotated or mirrored code either way, so this changes
    # no result today; on the luminance plane it costs a few milliseconds
    # and keeps every later step working on the picture the user saw.
    upright = ImageOps.exif_transpose(image)
    return upright if upright.mode == "L" else upright.convert("L")


def _scan(gray, pyzbar, deadline: float) -> set[bytes]:
    """Try the ladder until an attempt finds a code, time or attempts run out."""
    symbols = [pyzbar.ZBarSymbol.QRCODE]
    for attempt, candidate in enumerate(_scan_candidates(gray), start=1):
        found = {symbol.data for symbol in pyzbar.decode(candidate, symbols=symbols)}
        if found:
            # Several different codes in one attempt are reported as they
            # are, for read_qr_image to refuse. Later attempts are not
            # consulted: they look at the same pixels, only processed.
            return found
        if attempt >= _MAX_SCAN_ATTEMPTS or time.monotonic() >= deadline:
            break
    return set()


def _scan_candidates(gray):
    """Yield the images of the ladder, each built only when it is reached."""
    sources: dict[int, object] = {}
    tried: set[tuple[tuple[int, int], bool, str]] = set()
    for source, transform in _SCAN_LADDER:
        if source not in sources:
            sources[source] = (_rectify(gray) if source == _RECTIFIED
                               else _rescale(gray, source))
        base = sources[source]
        if base is None:
            continue
        # A small image comes out of several rescale targets unchanged.
        key = (base.size, source == _RECTIFIED, transform)
        if key in tried:
            continue
        tried.add(key)
        yield _transform(base, transform)


def _rescale(gray, longest: int):
    """The image with its longest side at `longest` (0: as it is).

    A request above the image's own size gives it unchanged, or enlarged
    to _MIN_LONGEST_SIDE if it is smaller than that.
    """
    from PIL import Image  # noqa: PLC0415 - see read_qr_image

    if not longest:
        return gray
    current = max(gray.size)
    target = longest
    if target >= current:
        # Never enlarged on request, only when too small to read at all.
        target = max(current, _MIN_LONGEST_SIDE)
    if target == current:
        return gray
    factor = target / current
    size = (max(round(gray.width * factor), 1), max(round(gray.height * factor), 1))
    # BOX averages every source pixel when shrinking, which also washes
    # out most of the moire; bicubic keeps module edges sharp when growing.
    method = Image.Resampling.BOX if factor < 1 else Image.Resampling.BICUBIC
    return gray.resize(size, method)


def _transform(gray, name: str):
    """One preprocessing of the ladder."""
    from PIL import ImageFilter, ImageOps  # noqa: PLC0415 - see read_qr_image

    if name == "plain":
        return gray
    if name == "smoothed":
        return _adaptive_threshold(gray.filter(ImageFilter.GaussianBlur(1)))
    binary = _adaptive_threshold(gray)
    if name == "inverted":
        # zbar reads dark modules on a light ground only, and a dark UI
        # theme would draw the code the other way round.
        return ImageOps.invert(binary)
    return binary


def _adaptive_threshold(gray):
    """Black where a pixel is darker than its neighbourhood, white elsewhere.

    A single global threshold cannot serve a screen that is bright on one
    side and dim on the other, or carries a reflection: comparing each
    pixel with its local mean can. Pillow's box blur is that local mean,
    computed in C with running sums, the integral-image trick in one pass.
    """
    import numpy as np  # noqa: PLC0415 - see read_qr_image
    from PIL import Image, ImageFilter  # noqa: PLC0415 - see read_qr_image

    pixels = np.asarray(gray, dtype=np.int16)
    local_mean = np.asarray(
        gray.filter(ImageFilter.BoxBlur(_ADAPTIVE_WINDOW // 2)), dtype=np.int16)
    light = pixels > local_mean - _ADAPTIVE_OFFSET
    return Image.fromarray(np.where(light, 255, 0).astype(np.uint8))


def _rectify(gray):
    """The QR region warped back to a square, or None if none stands out.

    zbar models perspective itself, but on a code this dense it gives up
    when the phone was held only 15 to 25 degrees off square to the
    screen, which is how people photograph a wall unit to dodge the glare.
    """
    import numpy as np  # noqa: PLC0415 - see read_qr_image
    from PIL import Image  # noqa: PLC0415 - see read_qr_image

    quad = _locate_qr(gray)
    if quad is None:
        return None
    centre = quad.mean(axis=0)
    quad = centre + (quad - centre) * (1 + _RECTIFIED_MARGIN)
    side = _RECTIFIED_SIDE
    square = np.array([[0, 0], [side, 0], [side, side], [0, side]], dtype=float)
    try:
        coefficients = _perspective_coefficients(square, quad)
    except np.linalg.LinAlgError:
        return None
    return gray.transform((side, side), Image.Transform.PERSPECTIVE,
                          tuple(coefficients), Image.Resampling.BILINEAR,
                          fillcolor=255)


def _locate_qr(gray):
    """Four corners, in image pixels, of the most QR-like region.

    A QR code is the one place in a photo where a small patch holds both
    very dark and very light pixels everywhere. The image is cut into
    blocks, each scored by its standard deviation; the region of high
    scores around the best block is taken, and the quadrilateral of
    largest area inside it is fitted, which is the code's outline under
    any rotation and perspective.
    """
    import numpy as np  # noqa: PLC0415 - see read_qr_image
    from PIL import Image  # noqa: PLC0415 - see read_qr_image

    factor = min(_LOCATE_LONGEST_SIDE / max(gray.size), 1.0)
    small = gray.resize((max(round(gray.width * factor), 1),
                         max(round(gray.height * factor), 1)),
                        Image.Resampling.BOX) if factor < 1 else gray
    pixels = np.asarray(small, dtype=np.float32)
    block = _LOCATE_BLOCK
    rows, cols = pixels.shape[0] // block, pixels.shape[1] // block
    if rows < 3 or cols < 3:
        return None
    blocks = pixels[:rows * block, :cols * block].reshape(rows, block, cols, block)
    spread = blocks.std(axis=(1, 3))
    # A finder pattern's centre or a run of same-coloured modules fills a
    # block with one colour; averaging with the neighbours closes the hole.
    padded = np.pad(spread, 1, mode="edge")
    spread = sum(padded[i:i + rows, j:j + cols]
                 for i in range(3) for j in range(3)) / 9
    if float(spread.max()) <= 0:
        return None
    candidate = spread > 0.5 * float(np.percentile(spread, 99.5))

    region = np.zeros_like(candidate)
    region[np.unravel_index(int(np.argmax(spread)), spread.shape)] = True
    while True:
        grown = region.copy()
        grown[1:] |= region[:-1]
        grown[:-1] |= region[1:]
        grown[:, 1:] |= region[:, :-1]
        grown[:, :-1] |= region[:, 1:]
        grown &= candidate
        if np.array_equal(grown, region):
            break
        region = grown

    ys, xs = np.nonzero(region)
    if len(xs) < 4:
        return None
    points = np.stack([(xs + 0.5) * block, (ys + 0.5) * block], axis=1) / factor
    return _largest_quadrilateral(points)


def _largest_quadrilateral(points):
    """Four of `points`, in order, enclosing about the largest area.

    Starts from the extremes along the diagonals or the axes, whichever
    encloses more (a square near 0 or near 45 degrees), then moves each
    corner in turn to the point farthest outside the opposite side.
    """
    import numpy as np  # noqa: PLC0415 - see read_qr_image

    x, y = points[:, 0], points[:, 1]
    starts = [
        [np.argmin(x + y), np.argmax(x - y), np.argmax(x + y), np.argmin(x - y)],
        [np.argmin(y), np.argmax(x), np.argmax(y), np.argmin(x)],
    ]

    def area(corners):
        cx, cy = corners[:, 0], corners[:, 1]
        return 0.5 * abs(np.dot(cx, np.roll(cy, -1)) - np.dot(cy, np.roll(cx, -1)))

    chosen = [int(i) for i in max(starts, key=lambda idx: area(points[idx]))]
    for _ in range(3):
        for corner in range(4):
            before = points[chosen[corner - 1]]
            after = points[chosen[(corner + 1) % 4]]
            opposite = points[chosen[(corner + 2) % 4]]
            edge = after - before
            outward = edge[0] * (y - before[1]) - edge[1] * (x - before[0])
            if edge[0] * (opposite[1] - before[1]) - edge[1] * (opposite[0] - before[0]) > 0:
                outward = -outward
            chosen[corner] = int(np.argmax(outward))
    return points[chosen]


def _perspective_coefficients(target, source):
    """Pillow's PERSPECTIVE data: maps each `target` corner onto `source`."""
    import numpy as np  # noqa: PLC0415 - see read_qr_image

    matrix = []
    values = []
    for (x, y), (u, v) in zip(target, source, strict=True):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.extend([u, v])
    return np.linalg.solve(np.array(matrix, dtype=float), np.array(values, dtype=float))
