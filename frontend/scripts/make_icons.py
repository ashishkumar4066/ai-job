"""Render the raster icons in public/ from public/favicon.svg.

    ../backend/.venv/Scripts/python.exe scripts/make_icons.py

One mark, one source file: favicon.svg is drawn by hand and everything else is
derived from it, so the tab icon, the ICO fallback and the iOS home-screen icon
cannot drift apart. Re-run this after editing the SVG.

Playwright (Chromium) is the only dependency, and the backend venv already has
it. Chromium rasterizes the SVG exactly as a browser would, supersamples and
reduces it on a canvas, and hands back PNG bytes; the ICO container around them
is 22 bytes of header per frame, which is not worth an imaging library. PNG
payloads inside an ICO are read by every browser and by Windows since Vista.

Two outputs:
  favicon.ico          16/32/48, for browsers and Windows shortcuts that ask
                       for an ICO rather than taking the SVG.
  apple-touch-icon.png 180x180, full-bleed. iOS masks the icon with its own
                       squircle, so this one is rendered with square corners:
                       rounded ones leave transparent slivers outside that
                       mask, composited against whatever sits behind them.
"""

from __future__ import annotations

import base64
import re
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

PUBLIC = Path(__file__).resolve().parent.parent / "public"
SOURCE = PUBLIC / "favicon.svg"

ICO_SIZES = (16, 32, 48)
APPLE_SIZE = 180
# Drawn this many times larger, then reduced on the canvas. Chromium's direct
# rasterization of a 3px stroke at 16px is harsher than a reduction from 4x.
SUPERSAMPLE = 4

# Runs in the page: SVG -> oversized canvas -> reduced canvas -> PNG bytes.
RENDER_JS = """
async ([dataUri, size, scale, opaque]) => {
  const img = new Image();
  img.src = dataUri;
  await img.decode();

  const big = document.createElement("canvas");
  big.width = big.height = size * scale;
  big.getContext("2d").drawImage(img, 0, 0, big.width, big.height);

  const out = document.createElement("canvas");
  out.width = out.height = size;
  const ctx = out.getContext("2d");
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(big, 0, 0, size, size);

  if (opaque) {
    // An icon iOS will composite itself must not carry an alpha channel it
    // could show through; the mark is full-bleed, so this only fixes edges.
    ctx.globalCompositeOperation = "destination-over";
    ctx.fillStyle = "#6222f2";
    ctx.fillRect(0, 0, size, size);
  }
  return out.toDataURL("image/png").split(",")[1];
}
"""


def _render(page, svg: str, size: int, *, opaque: bool = False) -> bytes:
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    encoded = page.evaluate(RENDER_JS, [data_uri, size, SUPERSAMPLE, opaque])
    return base64.b64decode(encoded)


def _ico(frames: dict[int, bytes]) -> bytes:
    """Pack PNG frames into an ICO, smallest first."""
    sizes = sorted(frames)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(header) + 16 * len(sizes)
    directory, payload = b"", b""
    for size in sizes:
        png = frames[size]
        # A 256px frame is recorded as 0; nothing here is that large, but the
        # rule is part of the format rather than a special case of ours.
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset
        )
        payload += png
        offset += len(png)
    return header + directory + payload


def main() -> None:
    svg = SOURCE.read_text(encoding="utf-8")
    square = re.sub(r'(<rect width="48" height="48") rx="13"', r"\1", svg)
    if square == svg:
        raise SystemExit("favicon.svg no longer has the rx=13 tile this script expects")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<body></body>")
        frames = {size: _render(page, svg, size) for size in ICO_SIZES}
        apple = _render(page, square, APPLE_SIZE, opaque=True)
        browser.close()

    (PUBLIC / "favicon.ico").write_bytes(_ico(frames))
    (PUBLIC / "apple-touch-icon.png").write_bytes(apple)
    print(f"wrote favicon.ico ({'/'.join(str(s) for s in ICO_SIZES)}) and apple-touch-icon.png")


if __name__ == "__main__":
    main()
