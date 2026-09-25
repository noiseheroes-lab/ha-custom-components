"""Tests for the Vimar QR payload decoder."""

import base64

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.vimar_intercom import qr


def build_payload(plaintext: str, key: bytes = b"K" * 32, iv: bytes = b"I" * 16) -> str:
    """Build a QR payload the way the Vimar app does: key | ciphertext | iv."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(key + ciphertext + iv).decode()


PLAINTEXT = (
    "ID=60901\n"
    "PWD=examplepassword\n"
    "CDOMAIN=abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud\n"
    "CPROXY=ipvdes.vimar.cloud\n"
    "PROXY=192.0.2.10\n"
    "GID=21\n"
    "MAC=00:00:5E:00:53:00\n"
    "PLANTTYPE=2FV2\n"
    "PC=40515\n"
)


def test_decrypt_payload_returns_plaintext():
    assert qr.decrypt_payload(build_payload(PLAINTEXT)) == PLAINTEXT


def test_parse_fields_newline_separated():
    fields = qr.parse_fields(PLAINTEXT)
    assert fields["ID"] == "60901"
    assert fields["CDOMAIN"] == "abcdef123456.FFFFFFFFFF.ipvdes.vimar.cloud"
    assert fields["GID"] == "21"


def test_parse_fields_ampersand_separated():
    fields = qr.parse_fields("ID=60901&PWD=secret&CDOMAIN=example.invalid")
    assert fields == {
        "ID": "60901",
        "PWD": "secret",
        "CDOMAIN": "example.invalid",
    }


def test_parse_fields_percent_decodes_values():
    assert qr.parse_fields("ID=a%20b")["ID"] == "a b"


def test_decode_qr_round_trip():
    fields = qr.decode_qr(build_payload(PLAINTEXT))
    assert fields["ID"] == "60901"
    assert fields["MAC"] == "00:00:5E:00:53:00"


def test_decode_qr_strips_surrounding_whitespace():
    fields = qr.decode_qr("  " + build_payload(PLAINTEXT) + "\n")
    assert fields["ID"] == "60901"


def test_decode_qr_rejects_invalid_base64():
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr("this is not base64 $$$")


def test_decode_qr_rejects_short_payload():
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(base64.b64encode(b"tooshort").decode())


def test_decode_qr_rejects_wrong_key():
    payload = build_payload(PLAINTEXT, key=b"K" * 32)
    raw = bytearray(base64.b64decode(payload))
    raw[0] ^= 0xFF  # corrupt the embedded key
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(base64.b64encode(bytes(raw)).decode())


@pytest.mark.parametrize("missing", ["ID", "PWD", "CDOMAIN"])
def test_decode_qr_requires_mandatory_fields(missing):
    lines = [ln for ln in PLAINTEXT.strip().split("\n")
             if not ln.startswith(missing + "=")]
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(build_payload("\n".join(lines)))


# Every message `qr.py` is allowed to raise. All of them are built from
# constants; none can carry anything decrypted from the payload. The
# previous version of this test built a payload that decrypts cleanly
# and fails only the missing-field check, so plaintext could have been
# added to the two raises beside it and the test would still have
# passed. Pinning the whole set is what makes that impossible.
SAFE_MESSAGES = {
    "payload is not valid base64",
    "payload is too short to be a Vimar QR code",
    "payload is too long to be a Vimar QR code",
    "payload has an unexpected length",
    "payload could not be decrypted",
}
MISSING_FIELDS_PREFIX = "QR code is missing required fields: "

SECRETS = ("examplepassword", "60901", "ipvdes.vimar.cloud",
           "00:00:5E:00:53:00", "40515")


def _corrupt_key(plaintext: str) -> str:
    """A payload whose embedded key no longer decrypts its ciphertext."""
    raw = bytearray(base64.b64decode(build_payload(plaintext)))
    raw[0] ^= 0xFF
    return base64.b64encode(bytes(raw)).decode()


def _truncated(plaintext: str) -> str:
    """A payload whose ciphertext is not a whole number of blocks."""
    raw = base64.b64decode(build_payload(plaintext))
    return base64.b64encode(raw[:-17] + raw[-16:]).decode()


@pytest.mark.parametrize("make_payload", [
    lambda text: "not base64 $$$",
    lambda text: base64.b64encode(b"tooshort").decode(),
    lambda text: build_payload(text) * 400,
    _corrupt_key,
    _truncated,
    lambda text: build_payload(text.replace("ID=60901\n", "")),
])
def test_no_decrypted_content_ever_reaches_the_error_message(make_payload):
    """Every failure mode, not just the one that never decrypts anything."""
    with pytest.raises(qr.QRDecodeError) as excinfo:
        qr.decode_qr(make_payload(PLAINTEXT))

    message = str(excinfo.value)
    assert (message in SAFE_MESSAGES
            or message.startswith(MISSING_FIELDS_PREFIX)), message
    for secret in SECRETS:
        assert secret not in message


def test_a_multi_megabyte_paste_is_refused_before_it_is_decrypted():
    """Decoding runs on the event loop during the config flow."""
    with pytest.raises(qr.QRDecodeError) as excinfo:
        qr.decode_qr("A" * (qr.MAX_PAYLOAD_LENGTH + 4))
    assert "too long" in str(excinfo.value)
