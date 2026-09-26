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

    Blocking and CPU-bound: run it in the executor. pyzbar and Pillow
    are imported here, not at module level, because a Home Assistant
    Core install without the native libzbar would otherwise fail to
    import the whole integration, text paste included.

    Several identical QR codes (or zbar reporting one twice) count as
    one. Several different ones are refused rather than guessed at: the
    wrong one could belong to another user of the same panel.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise QRImageTooLargeError
    if is_heic(data):
        raise QRImageHEICError

    try:
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
            image.load()
            symbols = pyzbar.decode(image, symbols=[pyzbar.ZBarSymbol.QRCODE])
    except QRImageTooLargeError:
        raise
    except Image.DecompressionBombError as err:
        raise QRImageTooLargeError from err
    except (OSError, SyntaxError, ValueError, pyzbar.PyZbarError) as err:
        raise QRImageUnreadableError from err

    found = {symbol.data for symbol in symbols}
    if not found:
        raise QRNotFoundError
    if len(found) > 1:
        raise QRAmbiguousError
    # Undecodable bytes become U+FFFD, which is not base64, so a QR that
    # is readable but not Vimar's fails in decode_qr like a bad paste.
    return found.pop().decode("utf-8", errors="replace")
