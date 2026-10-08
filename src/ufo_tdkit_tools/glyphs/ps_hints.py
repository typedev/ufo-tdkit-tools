# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Translating hand-made Glyphs PostScript hints into the Adobe UFO format.

Glyphs stores the stems and ghosts a designer placed by hand as structures under
`com.schriftgestaltung.hints`. Nothing on the way into a UFO writes the key the
PS Hints overlay reads, so that work sits in the file and is invisible: the
overlay shows what afdko recomputed instead.

This writes `com.adobe.type.autohint.v2` — the same key the binary importer uses
— from those structures, so the designer's own hinting can be seen and kept.

One flat hint set per glyph. Glyphs' hint list has no hint-replacement structure
to preserve, so inventing several sets would claim information the source does
not contain.
"""

import logging

logger = logging.getLogger(__name__)

HINTS_LIB_KEY = "com.schriftgestaltung.hints"
ADOBE_HINT_KEY_V2 = "com.adobe.type.autohint.v2"

# Adobe's ghost-hint convention: the width is the marker
GHOST_WIDTH_TOP = -20
GHOST_WIDTH_BOTTOM = -21

_GHOST_TYPES = {"TopGhost": GHOST_WIDTH_TOP, "BottomGhost": GHOST_WIDTH_BOTTOM}

_DEFAULT_POINT_TAG = "hintRef0000"


def _fmt(value) -> str:
    """Adobe stem strings hold plain numbers — integers where possible."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _stem_keyword(horizontal: bool) -> str:
    """`hstem` for a horizontal hint, `vstem` for a vertical one.

    A Glyphs *horizontal* hint constrains Y — it sits on a crossbar — which is
    exactly what `hstem` means in PostScript. Verified against real outlines:
    the ghosts of a hinted font land on its cap height and baseline under this
    reading, and the stems fall inside their glyph's own range.
    """
    return "hstem" if horizontal else "vstem"


def ufo_point_index(contour, glyphs_index: int) -> int:
    """Where a Glyphs node index lands in the converted UFO contour.

    **A closed contour is rotated by one on the way into a UFO.** glyphsLib says
    why in `builder/paths.py`: "In Glyphs.app, the starting node of a closed
    contour is always stored at the end of the nodes list", so it does
    `nodes.insert(0, nodes.pop())` before drawing. Reading the Glyphs index
    straight out of the UFO therefore returns the *previous* node — which is not
    an error anything reports, just a hint quietly attached to the wrong place.
    On a real hinted master that was all 297 ghosts; with the rotation, all 297
    resolve to the node the designer picked.

    An open contour is not rotated: its first node becomes the `move` and the
    rest follow in order.
    """
    points = contour.points
    if not points:
        raise IndexError("empty contour")
    # The modulo below rotates; it must never be allowed to *wrap* an index that
    # is simply out of range. A hint pointing past the end of a contour — the
    # node it named was deleted — has to fail so the caller can report it, not
    # quietly land on some other node.
    if not 0 <= glyphs_index < len(points):
        raise IndexError(f"node {glyphs_index} outside a contour of {len(points)} points")
    if points[0].type == "move":  # open contour — indices already line up
        return glyphs_index
    return (glyphs_index + 1) % len(points)


def _origin_position(glyph, origin, horizontal: bool) -> float | None:
    """Coordinate of the node a ghost hint hangs on.

    A ghost carries no `place`; it names a node as `[contour, point]` — in
    Glyphs' own node order — and takes that node's coordinate on the constrained
    axis.
    """
    try:
        contour_index, point_index = origin
        contour = glyph.contours[contour_index]
        point = contour.points[ufo_point_index(contour, point_index)]
    except (TypeError, ValueError, IndexError):
        return None
    return point.y if horizontal else point.x


def glyphs_hints_to_stems(glyph) -> list[str]:
    """Adobe stem strings for one glyph's Glyphs PostScript hints.

    TrueType hints (`TT*`) and component hints (Corner/Cap) are skipped — they
    are not stems and have nothing to say in this format.

    Args:
        glyph: A UFO glyph carrying `com.schriftgestaltung.hints`.

    Returns:
        Strings like `"hstem 261 140"` or `"hstem 700 -20"`, in source order.
        Empty when the glyph has no PostScript hints.
    """
    hints = glyph.lib.get(HINTS_LIB_KEY)
    if not hints:
        return []

    stems: list[str] = []
    for hint in hints:
        hint_type = hint.get("type")
        horizontal = bool(hint.get("horizontal"))
        keyword = _stem_keyword(horizontal)

        if hint_type == "Stem":
            place = hint.get("place")
            if not place or len(place) < 2:
                continue
            position, width = place[0], place[1]
            if position is None or width is None:
                continue
            stems.append(f"{keyword} {_fmt(position)} {_fmt(width)}")

        elif hint_type in _GHOST_TYPES:
            position = _origin_position(glyph, hint.get("origin"), horizontal)
            if position is None:
                continue
            stems.append(f"{keyword} {_fmt(position)} {_GHOST_TYPES[hint_type]}")

    return stems


def _first_oncurve_point(glyph):
    """The point a hint set anchors to, preferring one that is already named."""
    for contour in glyph.contours:
        for point in contour.points:
            if point.type is not None:  # None marks an off-curve point
                return point
    return None


def import_ps_hints(ufo_font) -> int:
    """Write Adobe hint data for every glyph with hand-made Glyphs PS hints.

    Args:
        ufo_font: A UFO font — anything whose glyphs expose `contours`, `lib`
            and named points (ufoLib2 in the importer's case).

    Returns:
        How many glyphs received hint data.

    The overlay draws the *processed* layer only, so what this writes shows up
    there as "Hints in default layer — import to processed layer first" until the
    user presses **Import all** in the PS Hints panel. Building the processed
    layer here is deliberately left alone: that layer is the autohinter's
    output, and the plugin already owns the step that promotes hints into it.
    """
    written = 0
    for glyph in ufo_font:
        stems = glyphs_hints_to_stems(glyph)
        if not stems:
            continue

        hint_set: dict = {"stems": stems}
        anchor = _first_oncurve_point(glyph)
        if anchor is not None:
            if not anchor.name:
                anchor.name = _DEFAULT_POINT_TAG
            hint_set["pointTag"] = anchor.name

        glyph.lib[ADOBE_HINT_KEY_V2] = {
            # An empty id disables the staleness check. The outline is exactly
            # what the designer hinted, so claiming a hash would only invite it
            # to go stale on the first edit.
            "formatVersion": "1",
            "id": "",
            "hintSetList": [hint_set],
        }
        written += 1

    if written:
        logger.info("Imported PostScript hints for %d glyph(s)", written)
    return written


def reanchor_ps_hints(ufo_font) -> int:
    """Point each hint set back at an existing point after outlines changed.

    Baking corner components rebuilds contours around the corner node; when
    that node was the one the hint set hangs on, its name goes with it and the
    ``pointTag`` dangles. The stems themselves are coordinates and stay valid.

    Returns:
        How many glyphs had their hint set re-anchored.
    """
    fixed = 0
    for glyph in ufo_font:
        hints = glyph.lib.get(ADOBE_HINT_KEY_V2)
        if not hints:
            continue
        names = {p.name for contour in glyph.contours for p in contour.points if p.name}
        changed = False
        for hint_set in hints.get("hintSetList", []):
            tag = hint_set.get("pointTag")
            if tag and tag in names:
                continue
            anchor = _first_oncurve_point(glyph)
            if anchor is None:
                continue
            if not anchor.name:
                anchor.name = tag or _DEFAULT_POINT_TAG
                names.add(anchor.name)
            hint_set["pointTag"] = anchor.name
            changed = True
        fixed += changed
    return fixed
