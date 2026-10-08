# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Baking Glyphs corner and cap components into outlines.

Glyphs applies a corner component when it draws and when it exports; the UFO it
converts to stores the untouched outline plus a note asking the compiler to apply
the corner later. An imported font therefore looks unlike its source until
somebody compiles it.

glyphsLib ships the filter that does the applying. This module runs it and then
removes what would make it run a second time — which is the part that is easy to
miss and expensive to get wrong.
"""

import logging
import math

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

logger = logging.getLogger(__name__)

HINTS_LIB_KEY = "com.schriftgestaltung.hints"
UFO2FT_FILTERS_KEY = "com.github.googlei18n.ufo2ft.filters"

CORNER_FILTER_NAME = "cornerComponents"
_COMPONENT_HINT_TYPES = frozenset({"Corner", "Cap"})

#: Fields of a Glyphs hint that name a node as `[contour, node]`.
_NODE_FIELDS = ("origin", "target", "other1", "other2")


def apply_corner_components(ufo_font) -> int:
    """Apply every corner/cap component in `ufo_font`, then disarm the pass.

    Args:
        ufo_font: A **ufoLib2** Font. The filter needs ufoLib2 objects — it
            fails on a fontParts `RFont` (`'tuple' object has no attribute
            'defaultLayer'`) and on a defcon `Font` (`'Point' object has no
            attribute 'type'`) — which is why this runs inside the converter,
            where `to_designspace()` has just produced ufoLib2 fonts, rather
            than on a font already open in the editor.

    Returns:
        How many glyphs the filter changed.

    Hints that name nodes by index (TrueType hints, ghost origins) are moved
    to the same nodes in the baked outline; see `bake_corner_components`,
    which also returns the warnings this function only logs.
    """
    count, warnings = bake_corner_components(ufo_font)
    for warning in warnings:
        logger.warning("%s (%s)", warning.message, warning.details)
    return count


def bake_corner_components(ufo_font) -> tuple[int, list[ConversionWarning]]:
    """`apply_corner_components`, plus the warnings about hints it moved.

    The cleanup after the filter is not optional. The filter reads the corner
    placement from each glyph's hints and those hints survive it, so a second
    run applies the same corner again — a 4-point rectangle goes to 7 points,
    then to 10. Worse, the font lib still asks ufo2ft to run `cornerComponents`
    at compile time, so an exported font would get every corner twice. Dropping
    the Corner/Cap hints and the filter registration makes both impossible.

    Baking replaces a corner node with several points, so every Glyphs hint
    that names a node by index — TrueType stems, anchors, aligns,
    interpolations, diagonals, deltas, and ghost origins — would land on a
    different node afterwards. The outline is snapshotted first and each such
    reference is moved to the node with the same coordinates in the baked
    outline (`_remap_node_hints`).

    Returns:
        (glyphs the filter changed, warnings about hints that sat on a node
        the bake replaced or could not be followed).
    """
    from glyphsLib.filters.cornerComponents import CornerComponentsFilter

    before = {glyph.name: _snapshot(glyph) for glyph in ufo_font if HINTS_LIB_KEY in glyph.lib}

    touched = CornerComponentsFilter()(ufo_font)
    if not touched:
        return 0, []

    _strip_component_hints(ufo_font)
    _unregister_corner_filter(ufo_font)

    warnings: list[ConversionWarning] = []
    for name in touched:
        if name in before and name in ufo_font:
            warnings += _remap_node_hints(ufo_font[name], before[name])

    logger.info("Applied corner components to %d glyph(s)", len(touched))
    return len(touched), warnings


def _snapshot(glyph) -> list[list[tuple[float, float, str | None]]]:
    """Each contour's points as `(x, y, type)`, in UFO order."""
    return [[(p.x, p.y, p.type) for p in contour.points] for contour in glyph.contours]


def _to_ufo_index(points, glyphs_index: int) -> int | None:
    """Glyphs node index -> UFO point index (closed contours are rotated by one).

    Same rule as `ps_hints.ufo_point_index`, on a snapshot.
    """
    if not 0 <= glyphs_index < len(points):
        return None
    if points[0][2] == "move":
        return glyphs_index
    return (glyphs_index + 1) % len(points)


def _to_glyphs_index(points, ufo_index: int) -> int:
    """UFO point index -> Glyphs node index; the inverse of `_to_ufo_index`."""
    if points[0][2] == "move":
        return ufo_index
    return (ufo_index - 1) % len(points)


def _match_points(old, new) -> tuple[list[int], set[int]]:
    """Map each old point index to a new one by coordinates.

    A bake leaves every node except the corner node where it was, so an exact
    coordinate match (on-curve to on-curve, off-curve to off-curve, each new
    point used once) finds them. A point with no match — the replaced corner
    node — goes to the nearest new point of the same kind.

    Returns:
        (new index for every old index, the old indices that had no match).
    """
    free: dict[tuple, list[int]] = {}
    for index, (x, y, kind) in enumerate(new):
        free.setdefault((x, y, kind is None), []).append(index)

    mapping: list[int] = []
    unmatched: set[int] = set()
    for index, (x, y, kind) in enumerate(old):
        candidates = free.get((x, y, kind is None))
        if candidates:
            mapping.append(candidates.pop(0))
            continue
        unmatched.add(index)
        same_kind = [i for i, p in enumerate(new) if (p[2] is None) == (kind is None)] or list(
            range(len(new))
        )
        mapping.append(min(same_kind, key=lambda i: math.hypot(new[i][0] - x, new[i][1] - y)))
    return mapping, unmatched


def _remap_node_hints(glyph, old_contours) -> list[ConversionWarning]:
    """Move the node references of `glyph`'s Glyphs hints onto the baked outline."""
    hints = glyph.lib.get(HINTS_LIB_KEY)
    if not hints:
        return []
    new_contours = _snapshot(glyph)

    if len(new_contours) != len(old_contours):
        # Not something a corner bake does; nothing safe to map onto.
        return [
            ConversionWarning(
                category="hints",
                severity=WarningSeverity.WARNING,
                message=f"Glyph '{glyph.name}': hints may point at the wrong nodes after "
                "baking corners",
                details=f"The bake changed the contour count ({len(old_contours)} -> "
                f"{len(new_contours)}), so node references were left as they were.",
            )
        ]

    matches = [_match_points(old, new) for old, new in zip(old_contours, new_contours)]
    moved_off_corner: list[str] = []

    for hint in hints:
        for field in _NODE_FIELDS:
            ref = hint.get(field)
            if not (
                isinstance(ref, list)
                and len(ref) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in ref)
            ):
                continue  # absent, or `target` = "up" / "down"
            contour_index, node_index = ref
            if not 0 <= contour_index < len(old_contours):
                continue
            old = old_contours[contour_index]
            old_ufo = _to_ufo_index(old, node_index)
            if old_ufo is None:
                continue
            mapping, unmatched = matches[contour_index]
            new_ufo = mapping[old_ufo]
            hint[field] = [contour_index, _to_glyphs_index(new_contours[contour_index], new_ufo)]
            if old_ufo in unmatched:
                moved_off_corner.append(f"{hint.get('type')} {field}")

    if not moved_off_corner:
        return []
    return [
        ConversionWarning(
            category="hints",
            severity=WarningSeverity.WARNING,
            message=f"Glyph '{glyph.name}': a hint sat on a corner node the bake replaced; "
            "moved to the nearest node",
            details=", ".join(moved_off_corner),
        )
    ]


def _strip_component_hints(ufo_font) -> None:
    """Remove Corner/Cap entries from every glyph's Glyphs hint list.

    Other hint types — the PostScript and TrueType ones — are left alone; they
    describe the outline rather than modify it.
    """
    for glyph in ufo_font:
        hints = glyph.lib.get(HINTS_LIB_KEY)
        if not hints:
            continue
        remaining = [h for h in hints if h.get("type") not in _COMPONENT_HINT_TYPES]
        if len(remaining) == len(hints):
            continue
        if remaining:
            glyph.lib[HINTS_LIB_KEY] = remaining
        else:
            del glyph.lib[HINTS_LIB_KEY]


def _unregister_corner_filter(ufo_font) -> None:
    """Stop ufo2ft applying the corners again at compile time.

    Leaves every other registered filter — notably `eraseOpenCorners`, which is
    a different pass and still wanted.
    """
    filters = ufo_font.lib.get(UFO2FT_FILTERS_KEY)
    if not filters:
        return
    remaining = [f for f in filters if f.get("name") != CORNER_FILTER_NAME]
    if len(remaining) == len(filters):
        return
    if remaining:
        ufo_font.lib[UFO2FT_FILTERS_KEY] = remaining
    else:
        del ufo_font.lib[UFO2FT_FILTERS_KEY]
