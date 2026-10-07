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

logger = logging.getLogger(__name__)

HINTS_LIB_KEY = "com.schriftgestaltung.hints"
UFO2FT_FILTERS_KEY = "com.github.googlei18n.ufo2ft.filters"

CORNER_FILTER_NAME = "cornerComponents"
_COMPONENT_HINT_TYPES = frozenset({"Corner", "Cap"})


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

    The cleanup afterwards is not optional. The filter reads the corner
    placement from each glyph's hints and those hints survive it, so a second
    run applies the same corner again — a 4-point rectangle goes to 7 points,
    then to 10. Worse, the font lib still asks ufo2ft to run `cornerComponents`
    at compile time, so an exported font would get every corner twice. Dropping
    the Corner/Cap hints and the filter registration makes both impossible.
    """
    from glyphsLib.filters.cornerComponents import CornerComponentsFilter

    touched = CornerComponentsFilter()(ufo_font)
    if not touched:
        return 0

    _strip_component_hints(ufo_font)
    _unregister_corner_filter(ufo_font)

    logger.info("Applied corner components to %d glyph(s)", len(touched))
    return len(touched)


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
