# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
PostScript hints parser for UFO fonts.

Reads PS hint data from three sources (in priority order):
1. processedglyphs layer -- com.adobe.type.autohint.v2 in the
   com.adobe.type.processedglyphs layer's glyph lib
2. autohint v2 -- com.adobe.type.autohint.v2 in default layer glyph lib
3. public.postscript.hints -- public.postscript.hints in default layer glyph lib

Stem string format (shared by both hint keys):
    "hstem <position> <width>"
    "vstem <position> <width>"
    "hstem3 <p0> <w0> <p1> <w1> <p2> <w2>"
    "vstem3 <p0> <w0> <p1> <w1> <p2> <w2>"

Ghost hints use special width values:
    width == -20  ->  top/right edge ghost
    width == -21  ->  bottom/left edge ghost
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ufo_tdkit_tools.constants import (
    ADOBE_HINT_KEY_V1,
    ADOBE_HINT_KEY_V2,
    PROCESSED_LAYER_NAME,
    PUBLIC_PS_HINT_KEY,
    compute_outline_hash,
)

logger = logging.getLogger(__name__)


class HintSource(Enum):
    """Where hint data was read from."""

    PROCESSED_LAYER = "processedglyphs"
    AUTOHINT_V2 = "autohint_v2"
    PUBLIC_PS = "public_ps"


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class PSHint:
    """A single stem hint or ghost hint."""

    type: str  # "hstem", "vstem", "hstem3", "vstem3"
    position: float
    width: float
    # For hstem3/vstem3: three (position, width) pairs
    pairs: list[tuple[float, float]] | None = None
    raw: str = ""  # Original stem string

    @property
    def is_horizontal(self) -> bool:
        return self.type in ("hstem", "hstem3")

    @property
    def is_vertical(self) -> bool:
        return self.type in ("vstem", "vstem3")

    @property
    def is_triple(self) -> bool:
        return self.type in ("hstem3", "vstem3")

    @property
    def is_ghost(self) -> bool:
        return self.width in (-20, -21)

    @property
    def is_top_ghost(self) -> bool:
        """Top/right edge ghost (width == -20)."""
        return self.width == -20

    @property
    def is_bottom_ghost(self) -> bool:
        """Bottom/left edge ghost (width == -21)."""
        return self.width == -21

    @property
    def end(self) -> float:
        """End position (position + width) for non-ghost hints."""
        if self.is_ghost:
            return self.position
        return self.position + self.width


@dataclass
class PSHintSet:
    """A set of active hints for a section of the outline."""

    point_tag: str | None = None  # e.g., "hintRef0000"
    point_coords: tuple[float, float] | None = None
    stems: list[PSHint] = field(default_factory=list)
    index: int = 0  # Position in hintSetList

    @property
    def hstems(self) -> list[PSHint]:
        return [s for s in self.stems if s.is_horizontal]

    @property
    def vstems(self) -> list[PSHint]:
        return [s for s in self.stems if s.is_vertical]

    @property
    def ghost_hints(self) -> list[PSHint]:
        return [s for s in self.stems if s.is_ghost]


@dataclass
class PSHintData:
    """Complete PS hint data for a glyph."""

    source: HintSource
    hint_sets: list[PSHintSet] = field(default_factory=list)
    flex_points: list[str] = field(default_factory=list)
    id_hash: str | None = None
    is_stale: bool = False
    format_version: str = "1"
    errors: list[str] = field(default_factory=list)

    @property
    def total_stems(self) -> int:
        """Total unique stems across all hint sets."""
        seen = set()
        for hs in self.hint_sets:
            for s in hs.stems:
                seen.add(s.raw)
        return len(seen)

    @property
    def has_hint_substitution(self) -> bool:
        return len(self.hint_sets) > 1


# ── Stem string parsing ──────────────────────────────────────────────────────


def parse_stem(stem_str: str) -> PSHint | None:
    """Parse a stem string like 'hstem 0 52' or 'hstem3 0 10 20 10 40 10'.

    Returns:
        PSHint or None if parsing fails.
    """
    parts = stem_str.strip().split()
    if len(parts) < 3:
        return None

    stem_type = parts[0]

    if stem_type in ("hstem", "vstem"):
        if len(parts) < 3:
            return None
        try:
            pos = float(parts[1])
            width = float(parts[2])
        except ValueError:
            return None
        return PSHint(type=stem_type, position=pos, width=width, raw=stem_str)

    elif stem_type in ("hstem3", "vstem3"):
        if len(parts) < 7:
            return None
        try:
            values = [float(p) for p in parts[1:7]]
        except ValueError:
            return None
        pairs = [
            (values[0], values[1]),
            (values[2], values[3]),
            (values[4], values[5]),
        ]
        return PSHint(
            type=stem_type,
            position=values[0],
            width=values[1],
            pairs=pairs,
            raw=stem_str,
        )

    return None


# ── Point name resolution ────────────────────────────────────────────────────


def build_point_map(glyph) -> dict[str, tuple[float, float]]:
    """Map point names to (x, y) coordinates in the glyph.

    Handles both contour points and component points.
    """
    point_map: dict[str, tuple[float, float]] = {}

    for contour in glyph:
        for point in contour.points:
            if point.name:
                point_map[point.name] = (point.x, point.y)

    return point_map


def build_point_map_from_layer(glyph_set, glyph_name: str) -> dict[str, tuple[float, float]]:
    """Map point names to coordinates from a fontTools GlyphSet.

    Used for processedglyphs layer where we read via fontTools.
    """
    point_map: dict[str, tuple[float, float]] = {}

    class _NameCollector:
        """PointPen that collects named points."""

        def beginPath(self, **kwargs):
            pass

        def endPath(self):
            pass

        def addPoint(self, pt, segmentType=None, smooth=None, name=None, **kwargs):
            if name:
                point_map[name] = pt

        def addComponent(self, glyphName, transformation, **kwargs):
            pass

    try:
        glyph_set.readGlyph(glyph_name, _NameCollector(), _NameCollector())
    except Exception:
        pass

    return point_map


# ── Hint data parsing ────────────────────────────────────────────────────────


def _parse_hint_dict(
    hint_dict: dict[str, Any],
    point_map: dict[str, tuple[float, float]],
) -> tuple[list[PSHintSet], list[str], list[str], str | None, str]:
    """Parse a hint dictionary (shared format for both hint keys).

    Args:
        hint_dict: The dict from glyph.lib under the hint key.
        point_map: Map of point names to coordinates.

    Returns:
        (hint_sets, flex_points, errors, id_hash, format_version)
    """
    errors: list[str] = []
    hint_sets: list[PSHintSet] = []
    flex_points: list[str] = []
    id_hash = hint_dict.get("id")
    format_version = hint_dict.get("formatVersion", "1")

    hint_set_list = hint_dict.get("hintSetList", [])
    if not isinstance(hint_set_list, list):
        errors.append("hintSetList is not a list")
        return hint_sets, flex_points, errors, id_hash, format_version

    for i, hs_dict in enumerate(hint_set_list):
        if not isinstance(hs_dict, dict):
            errors.append(f"hintSetList[{i}] is not a dict")
            continue

        point_tag = hs_dict.get("pointTag")
        point_coords = None
        if point_tag and point_tag in point_map:
            point_coords = point_map[point_tag]
        elif point_tag:
            errors.append(f"pointTag '{point_tag}' not found in outline")

        stems_raw = hs_dict.get("stems", [])
        stems: list[PSHint] = []
        for stem_str in stems_raw:
            if not isinstance(stem_str, str):
                errors.append(f"Invalid stem value: {stem_str}")
                continue
            hint = parse_stem(stem_str)
            if hint is not None:
                stems.append(hint)
            else:
                errors.append(f"Cannot parse stem: '{stem_str}'")

        hint_sets.append(
            PSHintSet(
                point_tag=point_tag,
                point_coords=point_coords,
                stems=stems,
                index=i,
            )
        )

    # Flex hints
    flex_list = hint_dict.get("flexList", [])
    if isinstance(flex_list, list):
        flex_points = [str(f) for f in flex_list]

    return hint_sets, flex_points, errors, id_hash, format_version


# ── Available sources detection ──────────────────────────────────────────────


def get_available_sources(glyph, font=None) -> list[HintSource]:
    """Detect which hint sources are available for the glyph.

    Args:
        glyph: fontParts glyph from the default layer.
        font: fontParts font (needed for processedglyphs layer check).

    Returns:
        List of available HintSource values (ordered by priority).
    """
    sources: list[HintSource] = []

    # Check processedglyphs layer
    if font is not None:
        try:
            layer_names = [layer.name for layer in font.layers]
            if PROCESSED_LAYER_NAME in layer_names:
                processed_layer = font.getLayer(PROCESSED_LAYER_NAME)
                if glyph.name in processed_layer:
                    pg = processed_layer[glyph.name]
                    if pg.lib.get(ADOBE_HINT_KEY_V2) or pg.lib.get(ADOBE_HINT_KEY_V1):
                        sources.append(HintSource.PROCESSED_LAYER)
        except Exception as e:
            logger.debug(f"Error checking processedglyphs layer: {e}")

    # Check default layer keys
    if hasattr(glyph, "lib"):
        if glyph.lib.get(ADOBE_HINT_KEY_V2) or glyph.lib.get(ADOBE_HINT_KEY_V1):
            sources.append(HintSource.AUTOHINT_V2)
        if glyph.lib.get(PUBLIC_PS_HINT_KEY):
            sources.append(HintSource.PUBLIC_PS)

    return sources


# ── Main parsing entry point ─────────────────────────────────────────────────


def parse_ps_hints(
    glyph,
    source: HintSource,
    font=None,
) -> PSHintData:
    """Parse PostScript hints for a glyph from a specific source.

    Args:
        glyph: fontParts glyph from the default layer.
        source: Which hint source to read from.
        font: fontParts font (required for processedglyphs layer).

    Returns:
        PSHintData with parsed hints, or empty data if source not available.
    """
    if source == HintSource.PROCESSED_LAYER:
        return _parse_from_processed_layer(glyph, font)
    elif source == HintSource.AUTOHINT_V2:
        return _parse_from_default_layer(glyph, ADOBE_HINT_KEY_V2)
    elif source == HintSource.PUBLIC_PS:
        return _parse_from_default_layer(glyph, PUBLIC_PS_HINT_KEY)
    else:
        return PSHintData(source=source, errors=[f"Unknown source: {source}"])


def _parse_from_default_layer(glyph, lib_key: str) -> PSHintData:
    """Parse hints from the default layer glyph.lib."""
    source = (
        HintSource.AUTOHINT_V2
        if lib_key in (ADOBE_HINT_KEY_V2, ADOBE_HINT_KEY_V1)
        else HintSource.PUBLIC_PS
    )

    hint_dict = glyph.lib.get(lib_key)
    # Fall back to v1 key for adobe hints
    if hint_dict is None and lib_key == ADOBE_HINT_KEY_V2:
        hint_dict = glyph.lib.get(ADOBE_HINT_KEY_V1)

    if not hint_dict or not isinstance(hint_dict, dict):
        return PSHintData(source=source, errors=["No hint data found"])

    point_map = build_point_map(glyph)
    hint_sets, flex_points, errors, id_hash, fmt_ver = _parse_hint_dict(
        hint_dict, point_map
    )

    # Check staleness
    is_stale = False
    if id_hash:
        current_hash = compute_outline_hash(glyph)
        is_stale = current_hash != id_hash

    return PSHintData(
        source=source,
        hint_sets=hint_sets,
        flex_points=flex_points,
        id_hash=id_hash,
        is_stale=is_stale,
        format_version=fmt_ver,
        errors=errors,
    )


def _parse_from_processed_layer(glyph, font) -> PSHintData:
    """Parse hints from the processedglyphs layer."""
    source = HintSource.PROCESSED_LAYER

    if font is None:
        return PSHintData(source=source, errors=["No font available"])

    try:
        layer_names = [layer.name for layer in font.layers]
        if PROCESSED_LAYER_NAME not in layer_names:
            return PSHintData(source=source, errors=["Processed layer not found"])

        processed_layer = font.getLayer(PROCESSED_LAYER_NAME)
        if glyph.name not in processed_layer:
            return PSHintData(
                source=source,
                errors=[f"Glyph '{glyph.name}' not in processed layer"],
            )

        processed_glyph = processed_layer[glyph.name]
    except Exception as e:
        return PSHintData(source=source, errors=[f"Error reading layer: {e}"])

    hint_dict = processed_glyph.lib.get(ADOBE_HINT_KEY_V2)
    if hint_dict is None:
        hint_dict = processed_glyph.lib.get(ADOBE_HINT_KEY_V1)
    if not hint_dict or not isinstance(hint_dict, dict):
        return PSHintData(source=source, errors=["No hint data in processed layer"])

    # Build point map from the processed glyph (it has hintRef names)
    point_map = build_point_map(processed_glyph)

    hint_sets, flex_points, errors, id_hash, fmt_ver = _parse_hint_dict(
        hint_dict, point_map
    )

    # Staleness check: compare id against the processed glyph outline
    is_stale = False
    if id_hash:
        current_hash = compute_outline_hash(processed_glyph)
        is_stale = current_hash != id_hash

    return PSHintData(
        source=source,
        hint_sets=hint_sets,
        flex_points=flex_points,
        id_hash=id_hash,
        is_stale=is_stale,
        format_version=fmt_ver,
        errors=errors,
    )


# ── Font-level hint statistics ────────────────────────────────────────────────


def get_source_counts(font) -> dict[str, int]:
    """Count glyphs with hints per source.

    Returns:
        {"processed": N, "v2": N, "public_ps": N}
    """
    counts = {"processed": 0, "v2": 0, "public_ps": 0}
    if font is None:
        return counts

    # Check processedglyphs layer
    try:
        layer_names = [layer.name for layer in font.layers]
        if PROCESSED_LAYER_NAME in layer_names:
            processed = font.getLayer(PROCESSED_LAYER_NAME)
            for glyph_name in processed.keys():
                g = processed[glyph_name]
                if g.lib.get(ADOBE_HINT_KEY_V2) or g.lib.get(ADOBE_HINT_KEY_V1):
                    counts["processed"] += 1
    except Exception:
        pass

    # Check default layer
    for glyph_name in font.keys():
        try:
            g = font[glyph_name]
            if g.lib.get(ADOBE_HINT_KEY_V2) or g.lib.get(ADOBE_HINT_KEY_V1):
                counts["v2"] += 1
            if g.lib.get(PUBLIC_PS_HINT_KEY):
                counts["public_ps"] += 1
        except Exception:
            continue

    return counts


def get_glyph_hint_status(glyph, font=None) -> str | None:
    """Check where hints exist for a glyph.

    Returns:
        "processed" -- hints in processedglyphs layer
        "v2" -- hints in default layer (autohint v2), not in processed
        "public_ps" -- only public.postscript.hints, needs import
        None -- no hints anywhere
    """
    # Check processedglyphs layer first
    if font is not None:
        try:
            layer_names = [layer.name for layer in font.layers]
            if PROCESSED_LAYER_NAME in layer_names:
                processed = font.getLayer(PROCESSED_LAYER_NAME)
                if glyph.name in processed:
                    pg = processed[glyph.name]
                    if pg.lib.get(ADOBE_HINT_KEY_V2) or pg.lib.get(ADOBE_HINT_KEY_V1):
                        return "processed"
        except Exception:
            pass

    # Check default layer
    if hasattr(glyph, "lib"):
        if glyph.lib.get(ADOBE_HINT_KEY_V2) or glyph.lib.get(ADOBE_HINT_KEY_V1):
            return "v2"
        if glyph.lib.get(PUBLIC_PS_HINT_KEY):
            return "public_ps"

    return None


def reload_processed_layer(font, glyph_names: list[str]) -> None:
    """Reload processedglyphs layer from disk after otfautohint.

    Handles three cases:
    1. Layer didn't exist before -> discover and load from disk
    2. Layer exists, new glyphs added -> discover and load new glyphs
    3. Layer exists, glyphs re-hinted -> reload updated data
    """
    try:
        naked_font = font.naked()
        layer_names = [l.name for l in naked_font.layers]

        if PROCESSED_LAYER_NAME not in layer_names:
            # Layer was created on disk by otfautohint but defcon doesn't
            # know about it yet. Register it from the disk glyphSet.
            from fontTools.ufoLib import UFOReader

            font_path = naked_font.path
            if font_path is None:
                return

            reader = UFOReader(font_path, validate=False)
            disk_layer_names = reader.getLayerNames()
            if PROCESSED_LAYER_NAME not in disk_layer_names:
                return  # Not on disk either

            glyph_set = reader.getGlyphSet(PROCESSED_LAYER_NAME)
            naked_font.layers.newLayer(PROCESSED_LAYER_NAME, glyph_set)
            logger.info("Discovered new processedglyphs layer from disk")
            return  # All glyphs loaded by newLayer

        # Layer already known -- reload glyphs
        processed = font.getLayer(PROCESSED_LAYER_NAME)
        naked_layer = processed.naked()

        # Discover new glyphs on disk
        gs = naked_layer._glyphSet
        if gs is not None:
            gs.rebuildContents()
            disk_glyphs = set(gs.contents.keys())
            memory_glyphs = set(naked_layer.keys())
            new_glyphs = disk_glyphs - memory_glyphs

            for name in new_glyphs:
                naked_layer.loadGlyph(name)

        # Reload existing glyphs that were re-hinted
        existing = [n for n in glyph_names if n in naked_layer]
        if existing:
            naked_layer.reloadGlyphs(existing)

    except Exception as e:
        logger.error(f"Error reloading processed layer: {e}")
