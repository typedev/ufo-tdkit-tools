# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Whole-font wrappers around per-glyph hint operations.

The per-glyph helpers in :mod:`ps_hints.converter` and :mod:`ps_hints.optimizer`
operate on a single glyph at a time. The pipeline needs to apply them to an
entire font; these wrappers handle the iteration, source resolution, and
font-level parameter extraction (stem snaps, units-per-em).
"""

from __future__ import annotations

import logging

from ufo_tdkit_tools.ps_hints.converter import (
    export_from_processed,
    import_to_processed,
    remove_hints,
)
from ufo_tdkit_tools.ps_hints.optimizer import apply_optimized_hints, optimize_hints
from ufo_tdkit_tools.ps_hints.parser import (
    HintSource,
    get_available_sources,
    parse_ps_hints,
)

logger = logging.getLogger(__name__)


_SOURCE_NAME_MAP: dict[HintSource, str] = {
    HintSource.PROCESSED_LAYER: "processed",
    HintSource.AUTOHINT_V2: "v2",
    HintSource.PUBLIC_PS: "public_ps",
}


def detect_font_source(font) -> HintSource | None:
    """Detect the whole-font hint source by priority.

    Scans every glyph in the default layer and returns the highest-priority
    source that has hints in at least one glyph. Priority order is
    ``PROCESSED_LAYER > AUTOHINT_V2 > PUBLIC_PS``.

    Args:
        font: fontParts ``RFont`` (or compatible).

    Returns:
        The chosen :class:`HintSource`, or ``None`` if no glyph has hints
        in any source.
    """
    has_processed = False
    has_v2 = False
    has_public = False

    for glyph in font:
        sources = get_available_sources(glyph, font=font)
        if HintSource.PROCESSED_LAYER in sources:
            has_processed = True
        if HintSource.AUTOHINT_V2 in sources:
            has_v2 = True
        if HintSource.PUBLIC_PS in sources:
            has_public = True

    if has_processed:
        return HintSource.PROCESSED_LAYER
    if has_v2:
        return HintSource.AUTOHINT_V2
    if has_public:
        return HintSource.PUBLIC_PS
    return None


def count_glyphs_with_source(font, source: HintSource) -> int:
    """Count glyphs in the default layer that have hints in the given source."""
    return sum(1 for glyph in font if source in get_available_sources(glyph, font=font))


def glyphs_missing_source(font, source: HintSource | None) -> list[str]:
    """Names of drawable glyphs that have no hints in ``source``.

    Empty glyphs (no contours and no components, e.g. ``space``) are excluded:
    there is nothing to hint, and the autohinter skips them anyway.

    Args:
        font: fontParts ``RFont`` (or compatible).
        source: Source to check, or ``None`` to treat every drawable glyph as
            unhinted (no source was detected at all).

    Returns:
        Glyph names in font order.
    """
    missing = []
    for glyph in font:
        if not len(glyph) and not len(glyph.components):
            continue
        if source is None or source not in get_available_sources(glyph, font=font):
            missing.append(glyph.name)
    return missing


def import_all_to_processed(font, source: HintSource | str) -> int:
    """Import hints from ``source`` into the processedglyphs layer for every glyph.

    No-op when ``source`` is ``PROCESSED_LAYER`` (hints are already there).

    Args:
        font: fontParts font.
        source: Source to read from. Either a :class:`HintSource` or one of
            ``"v2"``, ``"public_ps"``, ``"processed"``.

    Returns:
        Number of glyphs successfully imported.
    """
    src = _to_source_string(source)
    if src == "processed":
        return 0
    count = 0
    for glyph in font:
        if import_to_processed(glyph, font, src):
            count += 1
    return count


def export_all_from_processed(font, target: HintSource | str, names=None) -> int:
    """Export hints from processedglyphs to ``target``.

    Args:
        font: fontParts font.
        target: ``"v2"`` or ``"public_ps"`` (or the matching :class:`HintSource`).
        names: Optional iterable of glyph names to restrict the export to.
            ``None`` exports every glyph.

    Returns:
        Number of glyphs exported.
    """
    tgt = _to_source_string(target)
    if tgt == "processed":
        return 0
    selected = None if names is None else set(names)
    count = 0
    for glyph in font:
        if selected is not None and glyph.name not in selected:
            continue
        if export_from_processed(glyph, font, tgt):
            count += 1
    return count


def remove_all_hints(font, source: HintSource | str) -> int:
    """Remove hints from ``source`` for every glyph."""
    src = _to_source_string(source)
    count = 0
    for glyph in font:
        if remove_hints(glyph, font, src):
            count += 1
    return count


def optimize_font(font) -> dict[str, int]:
    """Optimize hints in processedglyphs for every glyph in the default layer.

    Reads each glyph's hints from the processedglyphs layer, runs them through
    :func:`ps_hints.optimizer.optimize_hints` with font-level parameters
    (stem snaps and units-per-em), and writes the result back to
    processedglyphs via :func:`apply_optimized_hints`.

    Args:
        font: fontParts font with hints already imported into processedglyphs.

    Returns:
        Dict with ``"optimized"`` and ``"skipped"`` glyph counts.
    """
    info = font.info
    stem_snap_v = list(info.postscriptStemSnapV) if info.postscriptStemSnapV else None
    stem_snap_h = list(info.postscriptStemSnapH) if info.postscriptStemSnapH else None
    upm = info.unitsPerEm or 1000

    optimized = 0
    skipped = 0

    for glyph in font:
        hint_data = parse_ps_hints(glyph, HintSource.PROCESSED_LAYER, font=font)
        if not hint_data.hint_sets:
            skipped += 1
            continue
        try:
            new_hd = optimize_hints(
                hint_data,
                glyph_width=glyph.width or 0,
                stem_snap_v=stem_snap_v,
                stem_snap_h=stem_snap_h,
                upm=upm,
                glyph=glyph,
            )
        except Exception as e:
            logger.warning(f"optimize_hints failed for '{glyph.name}': {e}")
            skipped += 1
            continue

        if apply_optimized_hints(glyph, font, new_hd):
            optimized += 1
        else:
            skipped += 1

    return {"optimized": optimized, "skipped": skipped}


def _to_source_string(source: HintSource | str) -> str:
    if isinstance(source, HintSource):
        return _SOURCE_NAME_MAP[source]
    return source
