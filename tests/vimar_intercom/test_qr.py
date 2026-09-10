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


def test_error_message_never_contains_plaintext():
    payload = build_payload(PLAINTEXT.replace("ID=60901\n", ""))
    with pytest.raises(qr.QRDecodeError) as excinfo:
        qr.decode_qr(payload)
    assert "examplepassword" not in str(excinfo.value)
