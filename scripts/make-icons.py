#!/usr/bin/env python3
# ABOUTME: Generates the web viewer's icons (PNG, stdlib only) — a white bell on a rounded teal square.
# ABOUTME: Run from the repo root after changing the design: python3 scripts/make-icons.py
import math
import os
import struct
import sys
import zlib

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
SIZES = {"icon-512.png": 512, "icon-192.png": 192, "apple-touch-icon.png": 180}
BACKGROUND = (15, 118, 110)   # teal
BELL = (255, 255, 255)
BADGE = (239, 68, 68)         # the unread dot
SUPERSAMPLE = 4


def in_rounded_square(x, y, radius=0.22):
    """Unit square with rounded corners of the given radius."""
    cx = min(max(x, radius), 1 - radius)
    cy = min(max(y, radius), 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def in_bell(x, y):
    """Bell silhouette in unit coordinates: dome, flared body, base bar, clapper."""
    # dome: upper half of a circle
    if y <= 0.44 and (x - 0.5) ** 2 + (y - 0.44) ** 2 <= 0.24 ** 2:
        return True
    # body: widens from the dome's radius to the base
    if 0.44 <= y <= 0.66:
        half = 0.24 + (y - 0.44) / (0.66 - 0.44) * 0.10
        if abs(x - 0.5) <= half:
            return True
    # base bar with rounded ends
    if 0.64 <= y <= 0.71:
        if abs(x - 0.5) <= 0.335:
            return True
        for ex in (0.5 - 0.335, 0.5 + 0.335):
            if (x - ex) ** 2 + (y - 0.675) ** 2 <= 0.035 ** 2:
                return True
    # clapper
    if (x - 0.5) ** 2 + (y - 0.77) ** 2 <= 0.065 ** 2:
        return True
    return False


def in_badge(x, y):
    return (x - 0.78) ** 2 + (y - 0.24) ** 2 <= 0.11 ** 2


def in_badge_ring(x, y):
    return (x - 0.78) ** 2 + (y - 0.24) ** 2 <= 0.135 ** 2


def color_at(x, y):
    """Colour and alpha of the point (x, y) in unit coordinates."""
    if not in_rounded_square(x, y):
        return (0, 0, 0, 0)
    if in_badge(x, y):
        return BADGE + (255,)
    if in_badge_ring(x, y):
        return BACKGROUND + (255,)  # a gap between the badge and the bell
    if in_bell(x, y):
        return BELL + (255,)
    return BACKGROUND + (255,)


def render(size):
    rows = []
    step = 1.0 / (size * SUPERSAMPLE)
    for py in range(size):
        row = bytearray([0])  # PNG filter byte: none
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx + 0.5) * step
                    y = (py * SUPERSAMPLE + sy + 0.5) * step
                    cr, cg, cb, ca = color_at(x, y)
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            n = SUPERSAMPLE * SUPERSAMPLE
            if a:
                row += bytes((round(r / a), round(g / a), round(b / a), round(a / n)))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))
    return b"".join(rows)


def png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def write_png(path, size):
    raw = render(size)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
           + png_chunk(b"IDAT", zlib.compress(raw, 9)) + png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def main():
    for name, size in SIZES.items():
        path = os.path.join(OUT_DIR, name)
        write_png(path, size)
        print(f"{name}: {size}x{size}, {os.path.getsize(path)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
