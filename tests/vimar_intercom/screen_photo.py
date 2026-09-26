"""Synthetic phone photos of a QR code shown on an LCD, for the image tests.

A real photo of the indoor unit's screen cannot be committed: the QR is
somebody's credentials. This builds the same kind of picture from a fake
payload, degraded the way a phone camera degrades a backlit screen:

- the LCD's RGB subpixel stripes and black matrix, which beat against the
  camera's own pixel grid and leave moire;
- a perspective warp, because nobody holds a phone square to a wall unit;
- defocus, sensor noise, a brightness gradient and a glare blob;
- JPEG compression, with the QR small inside a 12-megapixel frame;
- an EXIF orientation tag, as an iPhone writes for a portrait shot.

Everything is drawn from the `numpy.random.Generator` passed in, so a
seed reproduces a picture exactly.
"""

from __future__ import annotations

import io
import string
from dataclasses import dataclass

import numpy as np
import qrcode
from PIL import Image, ImageFilter

from test_qr import build_payload

PHOTO_SIZE = (4032, 3024)  # a 12 MP iPhone frame, landscape as the sensor sees it

_EC_LEVELS = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
}


def realistic_plaintext(rng: np.random.Generator, length: int) -> str:
    """Fake credentials padded with filler fields to about `length` bytes.

    A real code carries enough extra fields that its base64 is 350 to 500
    characters; the fixture in test_qr.py alone would give a small,
    unrealistically easy QR.
    """
    fields = [
        "ID=60901",
        "PWD=examplepassword",
        "CDOMAIN=abcdef123456.FFFFFFFFFF.ipvdes.example.invalid",
        "CPROXY=ipvdes.example.invalid",
        "PROXY=192.0.2.10",
        "GID=21",
        "PLANTTYPE=2FV2",
        "PC=40515",
    ]
    alphabet = string.ascii_letters + string.digits
    index = 0
    while len("\n".join(fields)) < length:
        value = "".join(rng.choice(list(alphabet), size=int(rng.integers(8, 24))))
        fields.append(f"X{index}={value}")
        index += 1
    return "\n".join(fields)[:length].rsplit("\n", 1)[0] + "\n"


def realistic_payload(rng: np.random.Generator) -> str:
    """A valid encrypted payload whose base64 is 350 to 500 characters."""
    while True:
        plaintext = realistic_plaintext(rng, int(rng.integers(220, 330)))
        key = bytes(rng.integers(0, 256, 32, dtype=np.uint8))
        iv = bytes(rng.integers(0, 256, 16, dtype=np.uint8))
        payload = build_payload(plaintext, key=key, iv=iv)
        if 350 <= len(payload) <= 500:
            return payload


def qr_matrix(text: str, ec: str = "L") -> np.ndarray:
    """The QR's modules as a bool array, True for dark, without quiet zone."""
    code = qrcode.QRCode(border=0, error_correction=_EC_LEVELS[ec])
    code.add_data(text)
    code.make(fit=True)
    return np.array(code.get_matrix(), dtype=bool)


@dataclass
class PhotoParams:
    """What a variant was drawn with, for reporting a failure."""

    version_modules: int
    lcd_px_per_module: int
    qr_photo_px: float
    keystone: float
    blur: float
    noise: float
    glare: float
    gradient: float
    jpeg_quality: int
    orientation: int


def _render_lcd(modules: np.ndarray, px_per_module: int, quiet: int,
                surround: int, rng: np.random.Generator,
                light_on_dark: bool) -> np.ndarray:
    """The screen area around the QR as its subpixels light up, float RGB.

    Each LCD pixel becomes a 3x3 block: one column per R, G, B stripe and
    a dimmer bottom row for the black matrix between pixel rows. At the
    sizes a phone photographs this, the stripe pitch sits close to the
    sensor pitch, which is what produces moire.
    """
    size = modules.shape[0]
    # Screen in LCD pixels: UI background, a white card, the QR on it.
    card = size + 2 * quiet
    total = card + 2 * surround
    screen = np.full((total, total), rng.uniform(0.15, 0.45), dtype=np.float32)
    black = rng.uniform(0.03, 0.12)
    ink, paper = (1.0, black) if light_on_dark else (black, 1.0)
    screen[surround:surround + card, surround:surround + card] = paper
    start = surround + quiet
    screen[start:start + size, start:start + size] = np.where(modules, ink, paper)
    pixels = np.kron(screen, np.ones((px_per_module, px_per_module), np.float32))

    tint = np.array([rng.uniform(0.85, 1.0), rng.uniform(0.9, 1.0), 1.0], np.float32)
    height, width = pixels.shape
    sub = np.zeros((height * 3, width * 3, 3), dtype=np.float32)
    for channel in range(3):
        sub[:, channel::3, channel] = np.repeat(pixels, 3, axis=0) * tint[channel]
    sub[2::3, :, :] *= 0.35
    # Each channel lights one column in three, and the black-matrix row is
    # dim: rescale so that white averages to 1 once the camera blurs it.
    return sub / ((1 + 1 + 0.35) / 9)


def _homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Coefficients mapping `dst` points back to `src`, as Pillow wants them."""
    rows = []
    rhs = []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs.extend([u, v])
    return np.linalg.solve(np.array(rows, float), np.array(rhs, float))


def screen_photo(text: str, rng: np.random.Generator, difficulty: float = 1.0,
                 light_on_dark: bool = False) -> tuple[bytes, PhotoParams]:
    """A JPEG that looks like a phone photo of `text` shown as a QR on an LCD.

    `difficulty` scales blur, noise, tilt, glare and how small the QR is;
    1.0 is a careless but not hopeless photo. `light_on_dark` draws the
    code the way a dark UI theme would, which zbar cannot read as is.
    """
    ec = "L" if rng.random() < 0.75 else "M"
    modules = qr_matrix(text, ec)
    size = modules.shape[0]
    px_per_module = int(rng.integers(4, 6))  # 85 modules x 5 px fits 480 px
    quiet = int(rng.integers(2, 5))
    surround = int(rng.integers(6, 20))
    lcd = _render_lcd(modules, px_per_module, quiet, surround, rng, light_on_dark)

    # Where the QR lands: its side in photo pixels, somewhere off centre.
    qr_side = rng.uniform(550, 1300)
    qr_side /= max(difficulty, 1.0) ** 0.5
    sub_per_module = px_per_module * 3
    scale = qr_side / (size * sub_per_module)
    lcd_h, lcd_w = lcd.shape[:2]
    half_w, half_h = lcd_w * scale / 2, lcd_h * scale / 2
    width, height = PHOTO_SIZE
    cx = rng.uniform(half_w + 40, width - half_w - 40)
    cy = rng.uniform(half_h + 40, height - half_h - 40)
    angle = np.deg2rad(rng.uniform(-12, 12))
    tilt = 0.12 * difficulty
    corners = np.array([[-half_w, -half_h], [half_w, -half_h],
                        [half_w, half_h], [-half_w, half_h]])
    corners *= 1 + rng.uniform(-tilt, tilt, size=(4, 2))
    rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    dst = corners @ rot.T + [cx, cy]
    sides = np.linalg.norm(dst - np.roll(dst, -1, axis=0), axis=1)
    # How far from square-on the shot is: the worse ratio of opposite sides.
    keystone = max(max(sides[0], sides[2]) / min(sides[0], sides[2]),
                   max(sides[1], sides[3]) / min(sides[1], sides[3]))

    # Work only on the part of the frame the screen covers; the rest is a
    # dim, featureless room, which costs nothing to draw.
    margin = 60
    left = int(max(dst[:, 0].min() - margin, 0))
    top = int(max(dst[:, 1].min() - margin, 0))
    right = int(min(dst[:, 0].max() + margin, width))
    bottom = int(min(dst[:, 1].max() + margin, height))
    region = (right - left, bottom - top)
    src = np.array([[0, 0], [lcd_w, 0], [lcd_w, lcd_h], [0, lcd_h]], float)
    coeffs = _homography(src, dst - [left, top])

    ambient = rng.uniform(0.02, 0.10)
    warped = []
    for channel in range(3):
        plane = Image.fromarray(lcd[:, :, channel], mode="F")
        # Bilinear without a prefilter: the stripes alias onto the sensor
        # grid exactly as they do through a lens that out-resolves them.
        out = plane.transform(region, Image.Transform.PERSPECTIVE, tuple(coeffs),
                              Image.Resampling.BILINEAR, fillcolor=0.0)
        warped.append(np.asarray(out))
    photo = np.stack(warped, axis=-1)

    blur = rng.uniform(0.4, 1.6) * difficulty
    rgb = Image.fromarray(np.clip(photo * 255, 0, 255).astype(np.uint8), mode="RGB")
    photo = np.asarray(rgb.filter(ImageFilter.GaussianBlur(blur)), np.float32) / 255

    # Exposure, a gradient across the screen, and a reflection.
    exposure = rng.uniform(0.8, 1.15)
    ys, xs = np.mgrid[0:region[1], 0:region[0]].astype(np.float32)
    theta = rng.uniform(0, 2 * np.pi)
    ramp = (xs * np.cos(theta) + ys * np.sin(theta))
    ramp = (ramp - ramp.min()) / max(float(np.ptp(ramp)), 1.0)
    gradient = min(0.55 * difficulty, 0.8) * rng.uniform(0.3, 1.0)
    photo *= (exposure * (1 - gradient * ramp))[..., None]
    glare = rng.uniform(0.0, 0.45) * difficulty
    gx, gy = rng.uniform(0, region[0]), rng.uniform(0, region[1])
    radius = rng.uniform(0.15, 0.4) * qr_side
    blob = np.exp(-((xs - gx) ** 2 + (ys - gy) ** 2) / (2 * radius ** 2))
    # Fade the reflection out towards the patch border so that pasting
    # the patch into the frame leaves no seam for the decoder to latch onto.
    window = np.minimum(np.minimum(xs, region[0] - 1 - xs),
                        np.minimum(ys, region[1] - 1 - ys)) / margin
    blob *= np.clip(window, 0, 1)
    photo += (glare * blob + ambient)[..., None]

    noise = rng.uniform(0.01, 0.04) * difficulty
    photo += rng.normal(0, 1, photo.shape).astype(np.float32) * (
        noise * np.sqrt(np.clip(photo, 0.02, None)))
    patch = np.clip(photo * 255, 0, 255).astype(np.uint8)

    frame = np.empty((height, width, 3), dtype=np.uint8)
    frame[...] = (np.array([ambient, ambient * 0.95, ambient * 0.9]) * 255).astype(np.uint8)
    frame[top:bottom, left:right] = patch
    image = Image.fromarray(frame, mode="RGB")

    # An iPhone stores the sensor's landscape pixels and says how to turn them.
    orientation = int(rng.choice([1, 6, 6, 3, 8]))
    stored = image.transpose({
        1: None, 3: Image.Transpose.ROTATE_180,
        6: Image.Transpose.ROTATE_90, 8: Image.Transpose.ROTATE_270,
    }[orientation]) if orientation != 1 else image
    exif = Image.Exif()
    exif[0x0112] = orientation
    quality = int(rng.integers(70, 93))
    buffer = io.BytesIO()
    stored.save(buffer, format="JPEG", quality=quality, exif=exif.tobytes())

    params = PhotoParams(
        version_modules=size, lcd_px_per_module=px_per_module,
        qr_photo_px=round(qr_side, 1), keystone=round(float(keystone), 3), blur=round(blur, 2),
        noise=round(noise, 3), glare=round(glare, 2), gradient=round(gradient, 2),
        jpeg_quality=quality, orientation=orientation)
    return buffer.getvalue(), params

