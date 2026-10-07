# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Restoring the delta amounts that glyphsLib drops on the way into a UFO.

Glyphs keeps a TrueType delta hint's per-ppm shifts in `GSHint.settings`, and
`glyphsLib.builder.hints.to_ufo_hints` copies a fixed list of fields that does
not include it. A `TTDelta` therefore arrives in the UFO as a position with no
amounts — enough to know a delta exists, not enough to show what it does.

This copies that one field back, **under Glyphs' own field name and in Glyphs'
own shape**, holding only the entry that belongs to this master. Nothing is
invented and nothing is translated; a round-trip back to `.glyphs` ignores the
field, since `to_glyphs_hints` reads a fixed field list too.

Deltas are authored per *instance*, so they are keyed by design-space location.
A master whose own location matches none of them simply has no deltas — that is
a real case, not an error.
"""

import logging

logger = logging.getLogger(__name__)

HINTS_LIB_KEY = "com.schriftgestaltung.hints"
MASTER_ORDER_LIB_KEY = "com.schriftgestaltung.fontMasterOrder"

# Glyphs pads a location to six axes regardless of how many the font declares
_LOCATION_AXES = 6

_DELTA_TYPE = "TTDelta"


def master_location_key(master, axis_count: int = _LOCATION_AXES) -> str:
    """The string Glyphs uses to key a delta set by design-space location.

    Glyphs writes `"{700, 300, 400, 0, 0, 0}"` — the master's axis values, padded
    with zeros to six. Verified against real sources: a master's own key appears
    verbatim among the delta locations alongside the instances'.
    """
    values = list(getattr(master, "axes", None) or [])
    values = values[:axis_count] + [0] * (axis_count - len(values))
    return "{" + ", ".join(str(int(v)) for v in values) + "}"


def master_for_ufo(gs_font, ufo_font):
    """The `GSFontMaster` a converted UFO came from, or None.

    glyphsLib records the master's index in the UFO's own lib. Matching by name
    or by design-space location would both be guesswork — an axis map can move a
    master's location, and brace-layer sources share a UFO with their master.
    """
    order = ufo_font.lib.get(MASTER_ORDER_LIB_KEY)
    if not isinstance(order, int):
        return None
    masters = gs_font.masters
    if 0 <= order < len(masters):
        return masters[order]
    return None


def _master_layer(gs_glyph, master_id: str):
    for layer in gs_glyph.layers:
        if layer.layerId == master_id:
            return layer
    return None


def restore_tt_delta_settings(gs_font, ufo_font) -> tuple[int, bool]:
    """Copy each TTDelta's amounts for this master back onto the UFO hint records.

    Args:
        gs_font: The parsed `GSFont` the UFO was built from.
        ufo_font: One converted master, as written by the conversion.

    Returns:
        `(restored, master_found)` — how many delta hints were given their
        amounts, and whether the UFO could be matched to a master at all.

    The UFO hint list and the Glyphs layer hint list are the same list in the
    same order — `to_ufo_hints` appends one dict per hint — so a delta is matched
    to its source by position among the hints of that glyph, not by guessing from
    its fields.
    """
    master = master_for_ufo(gs_font, ufo_font)
    if master is None:
        return 0, False

    location = master_location_key(master)
    restored = 0

    for gs_glyph in gs_font.glyphs:
        if gs_glyph.name not in ufo_font:
            continue
        ufo_hints = ufo_font[gs_glyph.name].lib.get(HINTS_LIB_KEY)
        if not ufo_hints:
            continue
        layer = _master_layer(gs_glyph, master.id)
        if layer is None:
            continue

        gs_hints = list(getattr(layer, "hints", []))
        if len(gs_hints) != len(ufo_hints):
            # The two lists should be one-to-one; if they ever are not, matching
            # by position would attach the wrong amounts to the wrong hint.
            logger.debug(
                "Glyph %r: %d Glyphs hints vs %d in the UFO — deltas skipped",
                gs_glyph.name,
                len(gs_hints),
                len(ufo_hints),
            )
            continue

        for gs_hint, ufo_hint in zip(gs_hints, ufo_hints):
            if ufo_hint.get("type") != _DELTA_TYPE:
                continue
            settings = getattr(gs_hint, "settings", None)
            if not settings:
                continue
            narrowed = _settings_for_location(settings, location)
            if narrowed:
                ufo_hint["settings"] = narrowed
                restored += 1

    if restored:
        logger.info("Restored delta amounts for %d TTDelta hint(s) at %s", restored, location)
    return restored, True


def _settings_for_location(settings, location: str) -> dict:
    """Keep only the delta entry that applies to one master's location.

    Preserves the nesting Glyphs uses — `{"deltaV": {location: {ppm: shift}}}` —
    so the record stays readable as what it is.
    """
    narrowed: dict = {}
    for direction, by_location in settings.items():
        if not isinstance(by_location, dict):
            continue
        per_ppm = by_location.get(location)
        if per_ppm:
            narrowed[direction] = {location: dict(per_ppm)}
    return narrowed
