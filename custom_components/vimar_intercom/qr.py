"""Decode the QR payload printed by the Vimar View app.

The QR encodes base64 of `key[32] || ciphertext || iv[16]`. The ciphertext
is AES-256-CBC with PKCS#7 padding, using the embedded key and IV. The
plaintext is `KEY=VALUE` pairs separated by newlines or `&`.

Reverse engineered from `com.vimar.vmsipsdk.utility.QrUtil`.

Nothing in this module logs. The decrypted payload contains the SIP
password and must never reach the log or an exception message.
"""

from __future__ import annotations

import base64
import binascii
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
