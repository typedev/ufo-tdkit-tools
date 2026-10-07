# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Convert PostScript hints between UFO formats.

All conversions go through the processedglyphs layer as the single
source of truth:

Import (into processed layer):
- public.postscript.hints -> processedglyphs layer
- com.adobe.type.autohint.v2 (default layer) -> processedglyphs layer

Export (from processed layer):
- processedglyphs layer -> public.postscript.hints (for FontLab)
- processedglyphs layer -> com.adobe.type.autohint.v2 (default layer)

Removal:
- Remove hints from any of the three sources
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from ufo_tdkit_tools.constants import (
    ADOBE_HINT_KEY_V1,
    ADOBE_HINT_KEY_V2,
    PROCESSED_LAYER_NAME,
    PUBLIC_PS_HINT_KEY,
    compute_outline_hash,
)

logger = logging.getLogger(__name__)

# Point name pattern matching afdko convention
HINT_REF_PATTERN = "hintRef%04d"


# ── Import: any source -> processedglyphs layer ───────────────────────────────


def import_to_processed(glyph, font, source: str) -> bool:
    """Import hints from a source into the processedglyphs layer.

    Args:
        glyph: fontParts glyph from the default layer.
        font: fontParts font object.
        source: "public_ps" or "v2"

    Returns:
        True if import succeeded.
    """
    if source == "public_ps":
        source_hints = glyph.lib.get(PUBLIC_PS_HINT_KEY)
    elif source == "v2":
        source_hints = glyph.lib.get(ADOBE_HINT_KEY_V2)
        if source_hints is None:
            source_hints = glyph.lib.get(ADOBE_HINT_KEY_V1)
    else:
        return False

    if not source_hints or not isinstance(source_hints, dict):
        return False

    # Ensure processedglyphs layer exists
    layer_names = [layer.name for layer in font.layers]
    if PROCESSED_LAYER_NAME not in layer_names:
        font.newLayer(PROCESSED_LAYER_NAME)

    processed_layer = font.getLayer(PROCESSED_LAYER_NAME)

    # Create/replace glyph in processed layer
    if glyph.name in processed_layer:
        del processed_layer[glyph.name]

    processed_glyph = processed_layer.newGlyph(glyph.name)
    processed_glyph.width = glyph.width or 0

    # Decompose components into flat contours
    _decompose_to_glyph(glyph, processed_glyph, font)

    # Strip smooth attributes
    for contour in processed_glyph.contours:
        for point in contour.points:
            if point.smooth:
                point.smooth = False

    # Strip original point names
    for contour in processed_glyph.contours:
        for point in contour.points:
            point.name = None

    # Copy and fix hint data
    hint_set_list = source_hints.get("hintSetList", [])
    if not isinstance(hint_set_list, list):
        return False

    flex_list = source_hints.get("flexList")
    flex_list = list(flex_list) if flex_list and isinstance(flex_list, list) else []

    # The names were stripped above, so carry each tag over to the point it
    # names in the default glyph -- assigning tags in on-curve order would move
    # every substitution point after the first.
    hint_set_list, flex_list = _remap_point_tags(glyph, processed_glyph, hint_set_list, flex_list)
    new_hint_set_list, _ = _fix_point_tags(processed_glyph, hint_set_list)

    new_hints: dict[str, Any] = {}
    if "formatVersion" in source_hints:
        new_hints["formatVersion"] = source_hints["formatVersion"]
    new_hints["hintSetList"] = new_hint_set_list

    if flex_list:
        new_hints["flexList"] = flex_list

    new_hints["id"] = compute_outline_hash(processed_glyph)

    processed_glyph.lib[ADOBE_HINT_KEY_V2] = new_hints

    logger.info(f"Imported {source} -> processed layer for '{glyph.name}'")
    return True


# ── Export: processedglyphs layer -> other format ─────────────────────────────


def export_from_processed(glyph, font, target: str) -> bool:
    """Export hints from processedglyphs layer to another format.

    Args:
        glyph: fontParts glyph from the default layer.
        font: fontParts font object.
        target: "public_ps" or "v2"

    Returns:
        True if export succeeded.
    """
    # Read hints from processedglyphs layer
    try:
        layer_names = [layer.name for layer in font.layers]
        if PROCESSED_LAYER_NAME not in layer_names:
            return False

        processed_layer = font.getLayer(PROCESSED_LAYER_NAME)
        if glyph.name not in processed_layer:
            return False

        processed_glyph = processed_layer[glyph.name]
        source_hints = processed_glyph.lib.get(ADOBE_HINT_KEY_V2)
        if source_hints is None:
            source_hints = processed_glyph.lib.get(ADOBE_HINT_KEY_V1)
        if not source_hints or not isinstance(source_hints, dict):
            return False
    except Exception:
        return False

    # Build export hint dict
    new_hints: dict[str, Any] = {}
    if "formatVersion" in source_hints:
        new_hints["formatVersion"] = source_hints["formatVersion"]

    # Point names are per layer: the autohinter renames the processed glyph's
    # points from scratch, while the default glyph may still carry names from
    # an earlier run on other points. Resolve tags by position, not by name --
    # a stale match starts the hint set mid-glyph and leaves the contours
    # drawn before it unhinted (an empty initial hintmask).
    hint_set_list = source_hints.get("hintSetList", [])
    flex_list = source_hints.get("flexList")
    flex_list = list(flex_list) if flex_list and isinstance(flex_list, list) else []
    hint_set_list, flex_list = _remap_point_tags(
        processed_glyph, glyph, copy.deepcopy(hint_set_list), flex_list
    )
    new_hint_set_list, _ = _fix_point_tags(glyph, hint_set_list)

    new_hints["hintSetList"] = new_hint_set_list

    if flex_list:
        new_hints["flexList"] = flex_list

    # Compute id from default layer glyph outline
    new_hints["id"] = compute_outline_hash(glyph)

    # Write to target
    if target == "public_ps":
        glyph.lib[PUBLIC_PS_HINT_KEY] = new_hints
        logger.info(f"Exported processed -> public.ps for '{glyph.name}'")
    elif target == "v2":
        glyph.lib[ADOBE_HINT_KEY_V2] = new_hints
        logger.info(f"Exported processed -> autohint v2 for '{glyph.name}'")
    else:
        return False

    return True


# ── Remove hints ─────────────────────────────────────────────────────────────


def remove_hints(glyph, font, source: str) -> bool:
    """Remove hints from a specific source.

    Args:
        glyph: fontParts glyph from the default layer.
        font: fontParts font object.
        source: "processed", "v2", or "public_ps"

    Returns:
        True if anything was removed.
    """
    removed = False

    if source == "processed":
        try:
            layer_names = [layer.name for layer in font.layers]
            if PROCESSED_LAYER_NAME in layer_names:
                processed = font.getLayer(PROCESSED_LAYER_NAME)
                if glyph.name in processed:
                    pg = processed[glyph.name]
                    for key in (ADOBE_HINT_KEY_V2, ADOBE_HINT_KEY_V1):
                        if key in pg.lib:
                            del pg.lib[key]
                            removed = True
        except Exception as e:
            logger.error(f"Error removing processed hints: {e}")

    elif source == "v2":
        for key in (ADOBE_HINT_KEY_V2, ADOBE_HINT_KEY_V1):
            if key in glyph.lib:
                del glyph.lib[key]
                removed = True

    elif source == "public_ps":
        if PUBLIC_PS_HINT_KEY in glyph.lib:
            del glyph.lib[PUBLIC_PS_HINT_KEY]
            removed = True

    return removed


# ── Internal helpers ──────────────────────────────────────────────────────────


def _remap_point_tags(
    src_glyph,
    dst_glyph,
    hint_set_list: list,
    flex_list: list[str],
) -> tuple[list, list[str]]:
    """Rewrite point names so they address the same points in ``dst_glyph``.

    Each name is looked up in ``src_glyph`` and resolved to its (contour,
    point) index. The point at that index in ``dst_glyph`` keeps its own name
    if it has one; otherwise it receives the source name, or a fresh
    ``hintRef`` name when the source name is already taken by another point
    of ``dst_glyph``. Names that do not resolve -- unknown in the source, or
    the outlines differ in structure -- are left as they are for
    :func:`_fix_point_tags`.

    Args:
        src_glyph: Glyph whose point names the hint data refers to.
        dst_glyph: Glyph the hint data is moving to (point names may be added).
        hint_set_list: ``hintSetList`` entries (dicts are copied, not mutated).
        flex_list: ``flexList`` point names.

    Returns:
        (new_hint_set_list, new_flex_list)
    """
    src_index: dict[str, tuple[int, int]] = {}
    for ci, contour in enumerate(src_glyph.contours):
        for pi, point in enumerate(contour.points):
            if point.name and point.name not in src_index:
                src_index[point.name] = (ci, pi)

    dst_contours = [list(c.points) for c in dst_glyph.contours]
    existing = {p.name for pts in dst_contours for p in pts if p.name}
    resolved: dict[str, str] = {}

    def resolve(name: str) -> str:
        if name in resolved:
            return resolved[name]
        loc = src_index.get(name)
        if loc is None or loc[0] >= len(dst_contours) or loc[1] >= len(dst_contours[loc[0]]):
            return name
        point = dst_contours[loc[0]][loc[1]]
        if not point.name:
            new_name = name
            counter = 0
            while new_name in existing:
                new_name = HINT_REF_PATTERN % counter
                counter += 1
            point.name = new_name
            existing.add(new_name)
        resolved[name] = point.name
        return point.name

    new_list = []
    for hs in hint_set_list:
        if isinstance(hs, dict) and hs.get("pointTag"):
            hs = dict(hs)
            hs["pointTag"] = resolve(hs["pointTag"])
        new_list.append(hs)
    new_flex = [resolve(n) if isinstance(n, str) else n for n in flex_list]
    return new_list, new_flex


def _fix_point_tags(
    glyph,
    hint_set_list: list[dict],
) -> tuple[list[dict], list[str]]:
    """Fix missing pointTags in hintSetList.

    For hint sets without a pointTag, assigns a hintRef name to the first
    available on-curve point.

    Args:
        glyph: fontParts glyph (may be modified -- point names added).
        hint_set_list: Original hintSetList from source hints.

    Returns:
        (new_hint_set_list, names_added)
    """
    new_list = []
    names_added: list[str] = []
    ref_counter = 0

    existing_names = set()
    for contour in glyph.contours:
        for point in contour.points:
            if point.name:
                existing_names.add(point.name)

    oncurve_points = []
    for contour in glyph.contours:
        for point in contour.points:
            if point.type != "offcurve":
                oncurve_points.append(point)

    oncurve_idx = 0

    for i, hs_dict in enumerate(hint_set_list):
        if not isinstance(hs_dict, dict):
            continue

        new_hs = copy.deepcopy(hs_dict)
        tag = new_hs.get("pointTag")

        if not tag:
            while True:
                candidate = HINT_REF_PATTERN % ref_counter
                ref_counter += 1
                if candidate not in existing_names:
                    break

            if i == 0 and oncurve_points:
                target_point = oncurve_points[0]
                oncurve_idx = 1
            elif oncurve_idx < len(oncurve_points):
                target_point = oncurve_points[oncurve_idx]
                oncurve_idx += 1
            else:
                new_list.append(new_hs)
                continue

            target_point.name = candidate
            existing_names.add(candidate)
            names_added.append(candidate)
            new_hs["pointTag"] = candidate
        else:
            if tag not in existing_names and oncurve_idx < len(oncurve_points):
                target_point = oncurve_points[oncurve_idx]
                oncurve_idx += 1
                target_point.name = tag
                existing_names.add(tag)
                names_added.append(tag)

        new_list.append(new_hs)

    return new_list, names_added


def _decompose_to_glyph(source_glyph, target_glyph, font) -> None:
    """Decompose source glyph into target glyph (flat contours)."""
    pen = target_glyph.getPen()
    try:
        source_glyph.draw(pen)
    except Exception as e:
        logger.error(f"Error decomposing glyph '{source_glyph.name}': {e}")
        for contour in source_glyph.contours:
            contour.draw(pen)
