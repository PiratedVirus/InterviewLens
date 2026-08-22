"""
One-time generator for the menu-bar icon set (icons/*.png).

Icons are drawn black-on-transparent and loaded as macOS "template" images
(see menubar_app.py's ICON_TEMPLATE / rumps `template=True`), so the system
recolors them to match the current menu bar (white in dark mode, black in
light mode) automatically -- same mechanism as the built-in Wi-Fi/Bluetooth
menu-bar icons. Re-run this file if you want to tweak the icon design; the
PNGs it produces are checked into icons/ and menubar_app.py just loads them.
"""

from pathlib import Path
from PIL import Image, ImageDraw

ICONS_DIR = Path(__file__).parent / "icons"
ICONS_DIR.mkdir(exist_ok=True)

# Draw at 4x and downsample for anti-aliased edges at real menu-bar size.
SUPERSAMPLE = 4
SIZE = 36 * SUPERSAMPLE  # final PNG is 36x36 (retina-sharp at 18pt)
CENTER = SIZE / 2


def _canvas():
    return Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))


def _save(img, name):
    img = img.resize((36, 36), Image.LANCZOS)
    img.save(ICONS_DIR / name)


def ring(radius_frac, stroke_frac):
    img = _canvas()
    draw = ImageDraw.Draw(img)
    r = SIZE * radius_frac
    w = SIZE * stroke_frac
    draw.ellipse(
        [CENTER - r, CENTER - r, CENTER + r, CENTER + r],
        outline=(0, 0, 0, 255),
        width=int(w),
    )
    return img


def dot(radius_frac):
    img = _canvas()
    draw = ImageDraw.Draw(img)
    r = SIZE * radius_frac
    draw.ellipse([CENTER - r, CENTER - r, CENTER + r, CENTER + r], fill=(0, 0, 0, 255))
    return img


def spinner(start_angle, stroke_frac=0.12, radius_frac=0.40, gap_deg=90):
    img = _canvas()
    draw = ImageDraw.Draw(img)
    r = SIZE * radius_frac
    w = SIZE * stroke_frac
    bbox = [CENTER - r, CENTER - r, CENTER + r, CENTER + r]
    draw.arc(bbox, start_angle, start_angle + (360 - gap_deg), fill=(0, 0, 0, 255), width=int(w))
    return img


# Idle: a plain ring -- "ready to record".
_save(ring(radius_frac=0.40, stroke_frac=0.12), "idle.png")

# Recording: a pulsing filled dot, two frames alternated by a timer.
_save(dot(radius_frac=0.38), "rec_a.png")
_save(dot(radius_frac=0.27), "rec_b.png")

# Processing: a 4-frame rotating ring (spinner). Kept as the fallback for
# work whose progress can't be measured.
for i, angle in enumerate([0, 90, 180, 270]):
    _save(spinner(start_angle=angle), f"proc_{i}.png")


def pie(fraction, radius_frac=0.40, stroke_frac=0.10):
    """Ring outline with a wedge filled clockwise from 12 o'clock."""
    img = _canvas()
    draw = ImageDraw.Draw(img)
    r = SIZE * radius_frac
    w = SIZE * stroke_frac
    draw.ellipse(
        [CENTER - r, CENTER - r, CENTER + r, CENTER + r],
        outline=(0, 0, 0, 255),
        width=int(w),
    )
    if fraction > 0:
        # Inset the wedge slightly so it doesn't bleed into the ring stroke.
        inner = r - w * 0.9
        draw.pieslice(
            [CENTER - inner, CENTER - inner, CENTER + inner, CENTER + inner],
            -90,
            -90 + 360 * min(fraction, 1.0),
            fill=(0, 0, 0, 255),
        )
    return img


# Progress: 0/12 .. 12/12 filled. The app picks the nearest frame for whatever
# fraction the pipeline reports, so the phase count can change freely.
PIE_STEPS = 12
for i in range(PIE_STEPS + 1):
    _save(pie(i / PIE_STEPS), f"pie_{i:02d}.png")

print("Wrote icons to", ICONS_DIR)
