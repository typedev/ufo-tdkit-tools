# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Shared constants and utilities for PS hint processing in UFO fonts.

Lib key constants follow the Adobe convention for storing PostScript hints
in UFO font glyph libraries. These keys are used across extraction,
optimization, and compilation modules.
"""

from __future__ import annotations

import hashlib

# ── Lib key constants ─────────────────────────────────────────────────────────

ADOBE_HINT_KEY_V2 = "com.adobe.type.autohint.v2"
ADOBE_HINT_KEY_V1 = "com.adobe.type.autohint"
PUBLIC_PS_HINT_KEY = "public.postscript.hints"
PROCESSED_LAYER_NAME = "com.adobe.type.processedglyphs"
PROCESSED_LAYER_NAME_ALT = "glyphs.com.adobe.type.processedglyphs"

# ── Validation constants ─────────────────────────────────────────────────────

VALID_STEM_TYPES = {"hstem", "vstem", "hstem3", "vstem3"}
MAX_STEMS_PER_HINTSET = 96


# ── ID hash computation ─────────────────────────────────────────────────────


def _norm_float(v: float) -> str:
    """Normalize float for hash: strip trailing .0."""
    r = round(v, 9)
    if r == int(r):
        return str(int(r))
    return repr(r)


def compute_outline_hash(glyph) -> str:
    """Compute outline hash matching afdko's HashPointPen algorithm.

    This is used to check if hints are stale (outline changed since hinting).

    Args:
        glyph: A fontParts-compatible glyph object with width, contours,
               and components attributes.

    Returns:
        Hash string: raw data string if < 128 chars, otherwise SHA-512 hex digest.
    """
    data_parts: list[str] = []

    # Width
    width = glyph.width if glyph.width is not None else 0
    data_parts.append(f"w{_norm_float(width)}")

    # Walk contours
    for contour in glyph:
        for point in contour.points:
            ptype = point.type or ""
            type_char = ptype[0] if ptype and ptype != "offcurve" else ""
            data_parts.append(
                f"{type_char}{_norm_float(point.x)}{_norm_float(point.y)}"
            )

    # Components
    for comp in glyph.components:
        xx, xy, yx, yy, dx, dy = comp.transformation
        data_parts.append(
            f"base:{comp.baseGlyph}"
            f"{_norm_float(xx)}{_norm_float(xy)}"
            f"{_norm_float(yx)}{_norm_float(yy)}"
            f"{_norm_float(dx)}{_norm_float(dy)}"
        )

    data = "".join(data_parts)
    if len(data) >= 128:
        data = hashlib.sha512(data.encode("ascii")).hexdigest()
    return data
