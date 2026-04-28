"""Generate extension toolbar icons (run once: python scripts/generate_icons.py from extension/)."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("pip install pillow", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "icons"


def icon_image(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 14)
    r = max(2, size // 6)
    # Dark panel
    draw.rounded_rectangle(
        [pad, pad, size - pad - 1, size - pad - 1],
        radius=r,
        fill=(15, 23, 50, 255),
        outline=(59, 130, 246, 200),
        width=max(1, size // 32),
    )
    # Accent cell (top-left area)
    cell = max(2, size // 5)
    x0, y0 = pad + size // 10, pad + size // 10
    draw.rounded_rectangle(
        [x0, y0, x0 + cell, y0 + cell],
        radius=max(1, cell // 4),
        fill=(59, 130, 246, 255),
    )
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        icon_image(size).save(OUT / f"icon-{size}.png", "PNG")
    print(f"Wrote PNGs to {OUT}")


if __name__ == "__main__":
    main()
