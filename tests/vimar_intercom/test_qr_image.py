"""Decode real QR images with pyzbar, the reader Home Assistant core ships.

Every test here needs the native libzbar. Where it cannot be loaded the
whole module is skipped rather than failed: nothing may be installed on
a developer machine just to run the suite.
"""

import io

import pytest

pytest.importorskip("PIL.Image")
qrcode = pytest.importorskip("qrcode")
try:
    # Not importorskip: with pyzbar installed and libzbar missing the
    # import raises a plain ImportError, which importorskip no longer
    # skips on (only ModuleNotFoundError), and an unloadable library is
    # an OSError it never catches.
    from pyzbar import pyzbar  # noqa: F401
except (ImportError, OSError) as err:
    pytest.skip(f"libzbar cannot be loaded: {err}", allow_module_level=True)

from PIL import Image  # noqa: E402

from custom_components.vimar_intercom import qr  # noqa: E402

from test_qr import PLAINTEXT, build_payload  # noqa: E402

PAYLOAD = build_payload(PLAINTEXT)


def qr_image(text: str, scale: int = 6) -> Image.Image:
    """A black-on-white QR code of `text`, built from qrcode's module matrix."""
    code = qrcode.QRCode(border=4)
    code.add_data(text)
    code.make(fit=True)
    matrix = code.get_matrix()
    size = len(matrix)
    image = Image.new("L", (size, size), 255)
    image.putdata([0 if cell else 255 for row in matrix for cell in row])
    return image.resize((size * scale, size * scale), Image.Resampling.NEAREST)


def encode(image: Image.Image, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def side_by_side(left: Image.Image, right: Image.Image) -> Image.Image:
    canvas = Image.new("L", (left.width + right.width, max(left.height, right.height)), 255)
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width, 0))
    return canvas


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_a_qr_image_decodes_to_the_same_credentials_as_its_text(fmt):
    text = qr.read_qr_image(encode(qr_image(PAYLOAD), fmt))
    assert text == PAYLOAD
    fields = qr.decode_qr(text)
    assert fields["ID"] == "60901"
    assert fields["PROXY"] == "192.0.2.10"


def test_a_qr_on_a_larger_colour_screenshot_is_found():
    screenshot = Image.new("RGB", (1170, 2532), (30, 30, 40))
    screenshot.paste(qr_image(PAYLOAD, scale=8).convert("RGB"), (150, 900))
    assert qr.read_qr_image(encode(screenshot)) == PAYLOAD


def test_an_image_without_a_qr_code_says_so():
    with pytest.raises(qr.QRNotFoundError):
        qr.read_qr_image(encode(Image.new("RGB", (400, 400), "white")))


def test_two_copies_of_the_same_qr_count_as_one():
    image = qr_image(PAYLOAD)
    assert qr.read_qr_image(encode(side_by_side(image, image))) == PAYLOAD


def test_two_different_qr_codes_are_refused_rather_than_guessed():
    other = build_payload(PLAINTEXT.replace("ID=60901", "ID=60902"))
    with pytest.raises(qr.QRAmbiguousError):
        qr.read_qr_image(encode(side_by_side(qr_image(PAYLOAD), qr_image(other))))


@pytest.mark.parametrize("data", [
    b"this is not an image at all",
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 200,
])
def test_bytes_that_are_not_an_image_are_unreadable(data):
    with pytest.raises(qr.QRImageUnreadableError):
        qr.read_qr_image(data)


def test_a_truncated_image_is_unreadable():
    data = encode(qr_image(PAYLOAD))
    with pytest.raises(qr.QRImageUnreadableError):
        qr.read_qr_image(data[: len(data) // 2])


def test_a_huge_canvas_in_a_small_file_is_refused_before_it_is_decoded():
    # About 70 MP of blank 1-bit pixels compresses to a few kilobytes.
    data = encode(Image.new("1", (9000, 8000), 1))
    assert len(data) < qr.MAX_IMAGE_BYTES
    with pytest.raises(qr.QRImageTooLargeError):
        qr.read_qr_image(data)


def test_a_readable_qr_that_is_not_vimar_fails_like_a_bad_paste():
    text = qr.read_qr_image(encode(qr_image("https://example.invalid/")))
    with pytest.raises(qr.QRDecodeError):
        qr.decode_qr(text)


def test_errors_carry_nothing_from_the_image():
    """The flow logs the class name only, but the message must be empty too."""
    with pytest.raises(qr.QRAmbiguousError) as excinfo:
        other = build_payload(PLAINTEXT.replace("PWD=examplepassword", "PWD=other"))
        qr.read_qr_image(encode(side_by_side(qr_image(PAYLOAD), qr_image(other))))
    assert str(excinfo.value) == ""
