"""Tests for the QR image path that need neither Pillow, pyzbar nor libzbar.

These run everywhere. The ones that decode a real image live in
test_qr_image.py and skip when the native zbar library is missing.
"""

import json
import sys
from pathlib import Path

import pytest

from custom_components.vimar_intercom import qr

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "vimar_intercom"


def _ftyp(major: bytes, *compatible: bytes) -> bytes:
    """The start of an ISO base media file: an `ftyp` box and some payload."""
    body = b"ftyp" + major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (len(body) + 4).to_bytes(4, "big") + body + b"\x00" * 64


def test_the_image_wins_when_both_fields_are_filled():
    assert qr.choose_qr_input("0123abcd", "pasted text") == ("image", "0123abcd")


def test_the_text_is_used_when_there_is_no_image():
    assert qr.choose_qr_input(None, "pasted text") == ("text", "pasted text")
    assert qr.choose_qr_input("", "pasted text") == ("text", "pasted text")


@pytest.mark.parametrize("pasted", [None, "", "   \n  "])
def test_neither_field_is_a_distinct_error(pasted):
    with pytest.raises(qr.QRInputMissingError):
        qr.choose_qr_input(None, pasted)


@pytest.mark.parametrize("data", [
    _ftyp(b"heic", b"mif1", b"heic"),
    _ftyp(b"mif1", b"mif1", b"heic"),
    _ftyp(b"heix"),
    _ftyp(b"hevc", b"mif1"),
])
def test_heic_is_recognised_from_its_brands(data):
    assert qr.is_heic(data)


@pytest.mark.parametrize("data", [
    _ftyp(b"avif", b"mif1", b"avif"),
    _ftyp(b"isom", b"mp41"),
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    b"\xff\xd8\xff\xe0" + b"\x00" * 64,
    b"",
    b"\x00\x00\x00\x08ftyp",
])
def test_other_formats_are_not_mistaken_for_heic(data):
    assert not qr.is_heic(data)


def test_a_heic_photo_is_refused_before_any_decoder_is_loaded(monkeypatch):
    # With the decoder unimportable, reaching the import would raise the
    # wrong error: HEIC has to be caught first, with its own message.
    monkeypatch.setitem(sys.modules, "pyzbar", None)
    with pytest.raises(qr.QRImageHEICError):
        qr.read_qr_image(_ftyp(b"heic", b"mif1", b"heic"))


def test_an_oversized_upload_is_refused_before_it_is_decoded(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyzbar", None)
    with pytest.raises(qr.QRImageTooLargeError):
        qr.read_qr_image(b"\x89PNG" + b"\x00" * qr.MAX_IMAGE_BYTES)


def test_a_missing_decoder_says_so_instead_of_failing(monkeypatch):
    """A Core install without libzbar must still offer the paste."""
    monkeypatch.setitem(sys.modules, "pyzbar", None)
    monkeypatch.setitem(sys.modules, "pyzbar.pyzbar", None)
    with pytest.raises(qr.QRReaderUnavailableError):
        qr.read_qr_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def test_importing_qr_does_not_load_the_decoder():
    """The lazy import is what keeps a host without libzbar working."""
    source = (COMPONENT / "qr.py").read_text()
    top_level = [line for line in source.splitlines()
                 if line.startswith(("import ", "from "))]
    assert not any("pyzbar" in line or "PIL" in line for line in top_level)


INPUT_ERRORS = [
    qr.QRInputMissingError, qr.QRImageTooLargeError, qr.QRImageHEICError,
    qr.QRImageUnreadableError, qr.QRNotFoundError, qr.QRAmbiguousError,
    qr.QRReaderUnavailableError,
]


@pytest.mark.parametrize("error", INPUT_ERRORS)
def test_every_input_error_has_a_message_in_every_language(error):
    for path in ("strings.json", "translations/en.json", "translations/it.json"):
        messages = json.loads((COMPONENT / path).read_text())["config"]["error"]
        assert messages.get(error.error_key), (path, error.error_key)


@pytest.mark.parametrize("error", INPUT_ERRORS)
def test_input_errors_are_not_mistaken_for_a_bad_payload(error):
    """The flow maps ValueError to `invalid_qr`; these must not match it."""
    assert not issubclass(error, ValueError)


def _keys(tree: dict, prefix: str = "") -> set[str]:
    keys = set()
    for key, value in tree.items():
        keys.add(prefix + key)
        if isinstance(value, dict):
            keys |= _keys(value, prefix + key + ".")
    return keys


def test_translations_have_exactly_the_keys_of_strings_json():
    reference = _keys(json.loads((COMPONENT / "strings.json").read_text()))
    for path in ("translations/en.json", "translations/it.json"):
        assert _keys(json.loads((COMPONENT / path).read_text())) == reference, path
