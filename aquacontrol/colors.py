"""Colour conversion between "#rrggbb" and the QUADRO's HSV: hue 0..1535 (1536 = 360 degrees), s and v 0..255."""
from __future__ import annotations

import colorsys
import re

HUE_STEPS = 1536
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def hex_to_hsv1536(color: str) -> tuple[int, int, int]:
    """"#rrggbb" -> (hue 0..1535, saturation 0..255, value 0..255). Raises ValueError for anything else."""
    if not isinstance(color, str) or not _HEX.fullmatch(color):
        raise ValueError(f"Farbe {color!r} ist kein #rrggbb-Wert")
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return round(h * HUE_STEPS) % HUE_STEPS, round(s * 255), round(v * 255)


def hsv1536_to_hex(h: int, s: int, v: int) -> str:
    """(hue 0..1535, saturation 0..255, value 0..255) -> "#rrggbb"."""
    r, g, b = colorsys.hsv_to_rgb((h % HUE_STEPS) / HUE_STEPS, min(max(s, 0), 255) / 255, min(max(v, 0), 255) / 255)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
