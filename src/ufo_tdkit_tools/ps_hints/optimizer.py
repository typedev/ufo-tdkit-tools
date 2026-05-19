# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
PostScript hints optimizer.

Cleans up autohinter output by collapsing multiple hint sets into a single
optimized set. Fixes common autohinter issues:

1. Overlapping vstems -- keeps leftmost on left side, rightmost on right side
2. Snap-incompatible vstems -- removes stems whose width is not within
   ~20% (floor 5u) of any stemSnap value
3. Centric glyphs -- for narrow glyphs (i, j, l), keeps narrower stem
4. Triple stem detection -- 3 equally-spaced similar-width vstems -> vstem3
5. Horizontal stems -- merges all hstems, deduplicates
6. Height coverage -- when glyph contours are available, overlapping stems
   are resolved by comparing their Y-extent on the actual contour (main
   stems win over small descender/ascender elements)

Pipeline order:
  collect stems -> separate ghosts/triples -> snap-compat filter
  -> accent-zone filter (Unicode NFD) -> coverage map -> small-element
  filter -> extract vstem3 (before overlaps!) -> resolve overlaps
  -> merge hstems
"""

from __future__ import annotations

import logging
from itertools import combinations
from typing import Any

from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2, PROCESSED_LAYER_NAME, compute_outline_hash
from ufo_tdkit_tools.ps_hints.parser import PSHint, PSHintData, PSHintSet

logger = logging.getLogger(__name__)


# ── Main entry point ─────────────────────────────────────────────────────────


def optimize_hints(
    hint_data: PSHintData,
    glyph_width: float,
    stem_snap_v: list[float] | None = None,
    stem_snap_h: list[float] | None = None,
    upm: int = 1000,
    glyph=None,
) -> PSHintData:
    """Optimize PS hints by collapsing hint sets into a single clean set.

    Args:
        hint_data: Parsed hint data from parser.
        glyph_width: Glyph advance width.
        stem_snap_v: Font-level vertical stem snap values.
        stem_snap_h: Font-level horizontal stem snap values.
        upm: Units per em.
        glyph: Optional fontParts glyph for contour-based height coverage.
            When provided, overlap resolution uses stem height coverage
            (Y-extent on contour) combined with stemSnap proximity.
            When None, falls back to position-based logic.

    Returns:
        New PSHintData with a single optimized hint set.
    """
    if not hint_data.hint_sets:
        return hint_data

    # Collect all unique stems across hint sets
    all_vstems = _collect_unique_stems(hint_data, vertical=True)
    all_hstems = _collect_unique_stems(hint_data, vertical=False)

    # Separate ghost hints (keep all, they don't conflict)
    ghost_hints = [s for s in all_vstems if s.is_ghost]
    ghost_hints += [s for s in all_hstems if s.is_ghost]
    all_vstems = [s for s in all_vstems if not s.is_ghost]
    all_hstems = [s for s in all_hstems if not s.is_ghost]

    # Skip existing triples from regular optimization (keep as-is)
    triple_vstems = [s for s in all_vstems if s.is_triple]
    triple_hstems = [s for s in all_hstems if s.is_triple]
    all_vstems = [s for s in all_vstems if not s.is_triple]
    all_hstems = [s for s in all_hstems if not s.is_triple]

    # Step 1: Remove vstems incompatible with stemSnapV
    if stem_snap_v:
        all_vstems = [s for s in all_vstems if _is_snap_compatible(s.width, stem_snap_v)]
    else:
        max_width = upm * 0.3
        all_vstems = [s for s in all_vstems if s.width <= max_width]

    # Step 2: Accent-zone filter (Unicode NFD-confirmed accented glyphs only)
    all_vstems, all_hstems = _apply_accent_zone_filter(all_vstems, all_hstems, glyph)

    # Step 3: Build coverage map if glyph contours are available
    coverage_map = _build_coverage_map(all_vstems, glyph)

    # Step 4: Filter small-element vstems (legs, tails, accent dots).
    # Geometric fallback for glyphs the Unicode-based filter missed
    # (non-Unicode, AGL-unknown, or composite legs that aren't accents).
    # MUST happen before vstem3 to prevent accent dots forming false triples.
    if coverage_map:
        all_vstems = _filter_small_element_vstems(all_vstems, coverage_map, glyph)
        coverage_map = {s.raw: coverage_map[s.raw] for s in all_vstems
                        if s.raw in coverage_map}

    # Step 5: Extract vstem3 candidates BEFORE overlap resolution
    all_vstems, new_triples = _extract_vstem3(all_vstems, stem_snap_v)
    triple_vstems.extend(new_triples)

    # Step 6: Optimize remaining vstems (overlap resolution)
    vstems = _optimize_vstems(all_vstems, glyph_width, stem_snap_v, upm, coverage_map)

    # Step 7: Optimize hstems (resolve overlaps)
    hstems = _optimize_hstems(all_hstems, stem_snap_h)

    # Build single hint set
    combined_stems: list[PSHint] = []
    combined_stems.extend(hstems)
    combined_stems.extend(triple_hstems)
    combined_stems.extend(vstems)
    combined_stems.extend(triple_vstems)
    combined_stems.extend(ghost_hints)

    optimized_set = PSHintSet(
        point_tag=None,
        point_coords=None,
        stems=combined_stems,
        index=0,
    )

    return PSHintData(
        source=hint_data.source,
        hint_sets=[optimized_set],
        flex_points=list(hint_data.flex_points),
        id_hash=hint_data.id_hash,
        is_stale=hint_data.is_stale,
        format_version=hint_data.format_version,
        errors=[],
    )


# ── Collect unique stems ─────────────────────────────────────────────────────


def _collect_unique_stems(hint_data: PSHintData, vertical: bool) -> list[PSHint]:
    """Collect unique stems across all hint sets."""
    seen: set[str] = set()
    result: list[PSHint] = []
    for hs in hint_data.hint_sets:
        for stem in hs.stems:
            if stem.is_vertical == vertical and stem.raw not in seen:
                seen.add(stem.raw)
                result.append(stem)
    return result


# ── Triple stem detection (before overlap resolution) ────────────────────────


def _extract_vstem3(
    vstems: list[PSHint],
    stem_snap_v: list[float] | None,
) -> tuple[list[PSHint], list[PSHint]]:
    """Extract vstem3 candidates from vstems BEFORE overlap resolution.

    Tests all C(N,3) combinations. N is typically < 10 so this is cheap.

    Adobe Type 1 spec requirements (T1_SPEC.pdf pp.53-54):
    1. Outer stem widths must be equal (tolerance 1.0 unit)
    2. Center-to-center spacing must be equal (tolerance 1.0 unit)
    3. Three stems must not overlap each other

    If multiple valid triples exist, picks the one with stems closest
    to stemSnap values.

    Returns:
        (remaining_vstems, new_triple_stems)
    """
    if len(vstems) < 3:
        return vstems, []

    best_combo: tuple[PSHint, ...] | None = None
    best_snap_score = float("inf")

    for combo in combinations(vstems, 3):
        sorted_combo = sorted(combo, key=lambda s: s.position)
        s0, s1, s2 = sorted_combo

        # No pairwise overlaps
        if _stems_overlap(s0, s1) or _stems_overlap(s1, s2) or _stems_overlap(s0, s2):
            continue

        # Constraint 1: outer widths must be equal
        if abs(s0.width - s2.width) > 1.0:
            continue

        # Constraint 2: center-to-center equal spacing
        c0 = s0.position + s0.width / 2.0
        c1 = s1.position + s1.width / 2.0
        c2 = s2.position + s2.width / 2.0
        if abs((c1 - c0) - (c2 - c1)) > 1.0:
            continue

        # Valid triple — score by stemSnap proximity (lower = better)
        if stem_snap_v:
            score = sum(_snap_distance(s.width, stem_snap_v) for s in sorted_combo)
        else:
            score = 0.0

        if best_combo is None or score < best_snap_score:
            best_combo = tuple(sorted_combo)
            best_snap_score = score

    if best_combo is None:
        return vstems, []

    # Build vstem3 hint
    pairs = [(s.position, s.width) for s in best_combo]
    pairs_str = " ".join(f"{p} {w}" for p, w in pairs)
    raw = f"vstem3 {pairs_str}"

    triple = PSHint(
        type="vstem3",
        position=best_combo[0].position,
        width=best_combo[0].width,
        pairs=pairs,
        raw=raw,
    )

    # Remove the 3 stems from the pool, plus any that touch/overlap the triple
    combo_raws = {s.raw for s in best_combo}
    remaining = []
    for s in vstems:
        if s.raw in combo_raws:
            continue
        # Filter stems that overlap or touch any triple component
        if any(_stems_touch_or_overlap(s, tc) for tc in best_combo):
            continue
        remaining.append(s)

    return remaining, [triple]


# ── Height coverage computation ──────────────────────────────────────────────


def _extract_segments(glyph) -> list[list[tuple[float, float]]]:
    """Extract contour segments from a fontParts glyph as tuple lists.

    Each segment is a list of (x, y) tuples:
    - 2 tuples: line segment
    - 3 tuples: quadratic curve
    - 4 tuples: cubic curve

    For composite glyphs (components only, no contours), the glyph is
    decomposed via DecomposingRecordingPen to expand all components.

    Returns:
        Flat list of all segments across all contours.
    """
    if glyph.contours:
        return _extract_segments_from_contours(glyph.contours)

    # Decompose composites
    if glyph.components:
        try:
            from fontParts.world import RGlyph
            from fontTools.pens.recordingPen import DecomposingRecordingPen
            drp = DecomposingRecordingPen(glyph.font)
            glyph.draw(drp)
            temp = RGlyph()
            drp.replay(temp.getPen())
            return _extract_segments_from_contours(temp.contours)
        except Exception:
            return []

    return []


def _compute_stem_height_coverage(
    stem: PSHint,
    segments: list[list[tuple[float, float]]],
) -> float:
    """Compute filled height of glyph contour at a stem's center X.

    Casts a vertical ray through the stem's center and measures the
    total filled length using even-odd fill rule. This correctly handles
    cases where unrelated contour elements (e.g., a horizontal bar)
    pass through the stem's X range at a different Y level — unlike
    bounding-box coverage, gaps between filled regions are excluded.

    Returns:
        Total filled height in font units, or 0.0 if no coverage.
    """
    # Sample at stem center with slight offset to avoid sitting on vertices
    x_sample = stem.position + stem.width / 2.0 + 0.5
    y_crossings = _collect_y_crossings(x_sample, segments)

    if len(y_crossings) < 2:
        return 0.0

    y_crossings.sort()

    # Even-odd fill: pair consecutive crossings
    total = 0.0
    for i in range(0, len(y_crossings) - 1, 2):
        total += y_crossings[i + 1] - y_crossings[i]

    return total


def _collect_y_crossings(
    x: float,
    segments: list[list[tuple[float, float]]],
) -> list[float]:
    """Collect all Y coordinates where contour segments cross vertical x=X."""
    from fontTools.misc.bezierTools import (
        calcCubicBounds,
        calcQuadraticBounds,
        curveLineIntersections,
    )

    y_crossings: list[float] = []
    y_lo = -100000.0
    y_hi = 100000.0
    vertical_line = [(x, y_lo), (x, y_hi)]

    for seg in segments:
        n = len(seg)

        # Quick bounding box check
        if n == 2:
            xs = (seg[0][0], seg[1][0])
            seg_x_min, seg_x_max = min(xs), max(xs)
        elif n == 3:
            bounds = calcQuadraticBounds(*seg)
            seg_x_min, seg_x_max = bounds[0], bounds[2]
        elif n == 4:
            bounds = calcCubicBounds(*seg)
            seg_x_min, seg_x_max = bounds[0], bounds[2]
        else:
            continue

        if seg_x_max < x or seg_x_min > x:
            continue

        if n == 2:
            y = _line_intersect_x(seg[0], seg[1], x)
            if y is not None:
                y_crossings.append(y)
        else:
            for isect in curveLineIntersections(seg, vertical_line):
                y_crossings.append(isect.pt[1])

    return y_crossings


def _line_intersect_x(
    pt1: tuple[float, float], pt2: tuple[float, float], x: float
) -> float | None:
    """Find Y where a line segment crosses x=X."""
    x1, y1 = pt1
    x2, y2 = pt2
    dx = x2 - x1
    if abs(dx) < 1e-10:
        return None
    t = (x - x1) / dx
    if 0.0 <= t <= 1.0:
        return y1 + t * (y2 - y1)
    return None


def _build_coverage_map(
    vstems: list[PSHint],
    glyph,
) -> dict[str, float] | None:
    """Build a coverage map for all vstems if glyph contours are available.

    Returns:
        Dict mapping stem.raw -> height coverage, or None if no glyph.
    """
    if glyph is None:
        return None

    try:
        segments = _extract_segments(glyph)
    except Exception:
        return None

    if not segments:
        return None

    coverage_map: dict[str, float] = {}
    for stem in vstems:
        coverage_map[stem.raw] = _compute_stem_height_coverage(stem, segments)

    return coverage_map


# ── Accent zone filtering (Unicode-based) ────────────────────────────────────


def _is_pua_codepoint(cp: int) -> bool:
    """Adobe Symbol Encoding maps some legacy glyph names to PUA codepoints."""
    return (
        0xE000 <= cp <= 0xF8FF
        or 0xF0000 <= cp <= 0xFFFFD
        or 0x100000 <= cp <= 0x10FFFD
    )


def _resolve_glyph_codepoint(glyph) -> int | None:
    """Get the glyph's primary non-PUA codepoint, falling back to AGL.

    For names like 'dcaron.alt', strips suffixes after the first dot before
    AGL lookup. Returns None for glyphs that map to PUA or aren't in AGL.
    """
    unis = getattr(glyph, "unicodes", None)
    if unis:
        cp = unis[0]
        if not _is_pua_codepoint(cp):
            return cp

    name = getattr(glyph, "name", None)
    if not name:
        return None
    base_name = name.split(".", 1)[0]
    try:
        from fontTools import agl
        s = agl.toUnicode(base_name)
    except Exception:
        return None
    if not s:
        return None
    cp = ord(s[0])
    if _is_pua_codepoint(cp):
        return None
    return cp


# Canonical combining classes (CCC) for above/below combining marks.
# Source: Unicode UAX #15 + DerivedCombiningClass.txt.
_ABOVE_CCC = frozenset({214, 216, 228, 230, 232, 234})
_BELOW_CCC = frozenset({200, 202, 218, 220, 222, 233, 240})

# Bar-like combining marks: their visual form *is* a horizontal stroke,
# so any hstem they produce is a real stem worth keeping. Other above/below
# accents (acute, grave, dieresis, cedilla, ogonek) have no meaningful
# hstem and stay subject to the zone filter.
_BAR_ACCENTS = frozenset({
    0x0304,  # COMBINING MACRON
    0x0305,  # COMBINING OVERLINE
    0x035E,  # COMBINING DOUBLE MACRON
    0x0331,  # COMBINING MACRON BELOW
    0x0332,  # COMBINING LOW LINE
    0x035F,  # COMBINING DOUBLE MACRON BELOW
})


def _glyph_accent_info(glyph) -> tuple[set[str], set[str], int | None]:
    """Detect accent positions via Unicode NFD decomposition.

    Classification uses the combining mark's canonical combining class
    (``unicodedata.combining``) rather than its name — this catches CEDILLA
    (CCC 202) and OGONEK (CCC 202) which sit below but lack 'BELOW' in
    their Unicode name.

    Returns:
        (positions, bar_positions, base_cp). ``positions`` ⊆
        {"above", "below"} is every detected accent zone. ``bar_positions``
        ⊆ positions is the subset whose accent is a horizontal bar
        (macron/overline/low-line); the hstem filter is bypassed in those
        zones. Empty positions ⇒ no accent detected.
    """
    import unicodedata

    cp = _resolve_glyph_codepoint(glyph)
    if cp is None:
        return set(), set(), None
    try:
        decomp = unicodedata.normalize("NFD", chr(cp))
    except (ValueError, TypeError):
        return set(), set(), None
    if len(decomp) < 2:
        return set(), set(), cp

    base_cp = ord(decomp[0])
    positions: set[str] = set()
    bar_positions: set[str] = set()
    for mark in decomp[1:]:
        if unicodedata.category(mark) != "Mn":
            continue
        ccc = unicodedata.combining(mark)
        if ccc in _ABOVE_CCC:
            positions.add("above")
            if ord(mark) in _BAR_ACCENTS:
                bar_positions.add("above")
        elif ccc in _BELOW_CCC:
            positions.add("below")
            if ord(mark) in _BAR_ACCENTS:
                bar_positions.add("below")
    return positions, bar_positions, base_cp


# Soft-dotted bases (Unicode Soft_Dotted.txt): their bare glyph carries a
# tittle that gets replaced by the accent in composites (idieresis, ї, etc).
# Using base.bounds.yMax would put the threshold above the accent — so for
# these bases we use xHeight instead. Only lowercase entries; uppercase
# variants (I, Ї, J, etc.) have no tittle and behave normally.
_SOFT_DOTTED_BASE = frozenset({
    0x0069,  # i  Latin small letter i
    0x006A,  # j  Latin small letter j
    0x012F,  # į  Latin small letter i with ogonek
    0x0249,  # ɉ  Latin small letter j with stroke
    0x0268,  # ɨ  Latin small letter i with stroke
    0x029D,  # ʝ  Latin small letter j with crossed-tail
    0x03F3,  # ϳ  Greek letter yot
    0x0456,  # і  Cyrillic Byelorussian-Ukrainian i
    0x0458,  # ј  Cyrillic je
    0x1E2D,  # ḭ  Latin small letter i with tilde below
    0x1ECB,  # ị  Latin small letter i with dot below
})


def _accent_zone_bounds(
    glyph,
    positions: set[str],
    base_cp: int | None,
) -> tuple[float | None, float | None]:
    """Y thresholds for accent-zone hint removal.

    Returns (above_y, below_y). Hint extent strictly above above_y is an
    above-accent; strictly below below_y is a below-accent.
    """
    import unicodedata

    BUFFER = 5.0
    above_y: float | None = None
    below_y: float | None = None

    font = getattr(glyph, "font", None)
    info = getattr(font, "info", None) if font is not None else None

    # Prefer the actual bounds of the base glyph in this font, except for
    # soft-dotted bases (i, j) under an above-accent: the tittle in the
    # bare base glyph isn't present in the composite.
    base_glyph = None
    base_via_bbox = base_cp is not None and not (
        "above" in positions and base_cp in _SOFT_DOTTED_BASE
    )
    if font is not None and base_via_bbox:
        try:
            for g in font:
                unis = getattr(g, "unicodes", None)
                if unis and unis[0] == base_cp:
                    base_glyph = g
                    break
        except Exception:
            base_glyph = None

    if base_glyph is not None:
        try:
            bounds = base_glyph.bounds
        except Exception:
            bounds = None
        if bounds is not None:
            if "above" in positions:
                above_y = bounds[3] + BUFFER
            if "below" in positions:
                below_y = bounds[1] - BUFFER
            # If both positions needed but bbox only covers one side, fall
            # through to fill the other via font metrics
            if (
                ("above" in positions) == (above_y is not None)
                and ("below" in positions) == (below_y is not None)
            ):
                return above_y, below_y

    # Fallback to font metrics
    if info is None:
        return above_y, below_y

    if "above" in positions and above_y is None:
        cat = None
        if base_cp is not None:
            try:
                cat = unicodedata.category(chr(base_cp))
            except (ValueError, TypeError):
                cat = None
        if cat == "Lu":
            top = getattr(info, "capHeight", None)
        elif base_cp in _SOFT_DOTTED_BASE:
            # i/j: accent replaces the tittle, so xHeight is the real top
            top = getattr(info, "xHeight", None) or getattr(info, "ascender", None)
        else:
            # Other Ll/Lt/unknown — ascender is safer than xHeight for ascenders
            top = getattr(info, "ascender", None)
        if top is not None:
            above_y = top + BUFFER

    if "below" in positions and below_y is None:
        below_y = -BUFFER

    return above_y, below_y


def _flat_contour_points(glyph) -> list[tuple[float, float]]:
    """All contour points (on-curve + off-curve) of the glyph, with
    components decomposed via ``DecomposingRecordingPen``.

    Returns a flat list of ``(x, y)`` tuples.
    """
    if glyph is None:
        return []
    try:
        if len(glyph) > 0:
            contours = list(glyph.contours)
        elif glyph.components:
            from fontParts.world import RGlyph
            from fontTools.pens.recordingPen import DecomposingRecordingPen
            drp = DecomposingRecordingPen(glyph.font)
            glyph.draw(drp)
            temp = RGlyph()
            drp.replay(temp.getPen())
            contours = list(temp.contours)
        else:
            return []
    except Exception:
        return []

    pts: list[tuple[float, float]] = []
    for c in contours:
        try:
            for p in c.points:
                pts.append((p.x, p.y))
        except Exception:
            continue
    return pts


def _vstem_edge_y_values(
    vstem: PSHint,
    points: list[tuple[float, float]],
    x_tolerance: float = 3.0,
) -> list[float]:
    """Y values of contour points whose X is near the vstem's edges.

    Returns the Y coordinates of points lying within ``x_tolerance`` of
    either ``vstem.position`` (left edge) or ``vstem.position+vstem.width``
    (right edge). These points define the Y-extent of the vertical stroke
    the vstem represents.
    """
    left_x = vstem.position
    right_x = vstem.position + vstem.width
    return [
        y for (x, y) in points
        if abs(x - left_x) <= x_tolerance or abs(x - right_x) <= x_tolerance
    ]


def _apply_accent_zone_filter(
    vstems: list[PSHint],
    hstems: list[PSHint],
    glyph,
) -> tuple[list[PSHint], list[PSHint]]:
    """Remove h/v hints located in the glyph's accent zone.

    Triggers only when Unicode NFD decomposition confirms the glyph carries
    an above and/or below combining mark. The cut zone is taken from the
    base glyph's actual bounding box (when the base glyph exists in the
    UFO) or from font metrics as a fallback.

    Bar-shaped marks (macron/overline/low-line — see ``_BAR_ACCENTS``) are
    themselves horizontal strokes: the hstem filter is skipped in any zone
    whose accent is a bar so the bar's own hstem survives. The vstem filter
    still runs unconditionally — bars produce no meaningful vstem.

    hstems
        Direct test on Y (hstem position/end are Y coordinates).

    vstems
        For each vstem, find every contour point whose X is within ±3 units
        of the stem's left or right edge — these points sit on the vertical
        edges of the stroke the stem represents. If **all** such points'
        Y values fall in the accent zone (``>= above_y`` or ``<= below_y``
        with a small margin), the stem belongs to the accent and is dropped.

        This works in three regimes:

        * **Separate-contour accents** (decomposed `Adieresis` dots):
          the dot's left/right X has only dot contour points, all above
          capHeight → removed.
        * **Integrated accents** (`Ccedilla`/`aogonek`: cedilla/ogonek
          fused with the base contour): the accent's vertical edges sit
          at X values where the base contour has no points (or has points
          only inside the base body). All edge points are in the accent
          zone → removed.
        * **Base body stems** under any accent (`Iacute` I-body vstem):
          the I-body's left/right X have points from baseline to capHeight
          — not all in the accent zone → kept.
    """
    if glyph is None:
        return vstems, hstems

    positions, bar_positions, base_cp = _glyph_accent_info(glyph)
    if not positions:
        return vstems, hstems

    above_y, below_y = _accent_zone_bounds(glyph, positions, base_cp)
    if above_y is None and below_y is None:
        return vstems, hstems

    # hstems: position/end are Y. The whole stem must sit in the accent zone.
    # In zones occupied by a bar-shaped mark (macron/overline/low-line) the
    # accent itself is a horizontal stroke — keep its hstem.
    hstem_above_y = None if "above" in bar_positions else above_y
    hstem_below_y = None if "below" in bar_positions else below_y
    new_hstems: list[PSHint] = []
    for s in hstems:
        if hstem_above_y is not None and s.position >= hstem_above_y:
            logger.debug(f"Drop above-accent hstem {s.raw}")
            continue
        if hstem_below_y is not None and s.end <= hstem_below_y:
            logger.debug(f"Drop below-accent hstem {s.raw}")
            continue
        new_hstems.append(s)

    # vstems: check Y of contour points on the stem's vertical edges
    points = _flat_contour_points(glyph)
    if not points:
        return vstems, new_hstems

    MARGIN_Y = 5.0
    X_TOLERANCE = 3.0
    new_vstems: list[PSHint] = []
    for s in vstems:
        edge_ys = _vstem_edge_y_values(s, points, X_TOLERANCE)
        if not edge_ys:
            new_vstems.append(s)
            continue

        in_above = (
            above_y is not None
            and all(y >= above_y - MARGIN_Y for y in edge_ys)
        )
        in_below = (
            below_y is not None
            and all(y <= below_y + MARGIN_Y for y in edge_ys)
        )
        if in_above or in_below:
            logger.debug(f"Drop accent vstem {s.raw}")
            continue
        new_vstems.append(s)

    return new_vstems, new_hstems


# ── Small element filtering ──────────────────────────────────────────────────


def _get_accent_reference_y(glyph) -> float | None:
    """Get the reference Y height that separates main body from accent zone.

    For uppercase (Lu): capHeight.
    For lowercase (Ll, Lt): xHeight.
    Returns None if font metrics are unavailable.
    """
    import unicodedata

    uni = getattr(glyph, "unicode", None)
    if uni is None:
        unis = getattr(glyph, "unicodes", None)
        if unis:
            uni = unis[0]
    if uni is None:
        return None

    try:
        category = unicodedata.category(chr(uni))
    except (ValueError, TypeError):
        return None

    font = getattr(glyph, "font", None)
    if font is None:
        return None
    info = getattr(font, "info", None)
    if info is None:
        return None

    if category == "Lu":
        return getattr(info, "capHeight", None)
    elif category in ("Ll", "Lt"):
        return getattr(info, "xHeight", None)

    return None


def _is_accent_zone_stem(
    stem: PSHint,
    segments: list[list[tuple[float, float]]],
    reference_y: float,
) -> bool:
    """Check if a stem's coverage is predominantly in the accent zone.

    Main body zone: [0, reference_y] (baseline to capHeight/xHeight).
    Accent zones: above reference_y (dieresis, acute, etc.)
                  or below 0 (cedilla, ogonek, etc.)

    Returns True if >70% of the stem's filled height is in accent zones.
    """
    x = stem.position + stem.width / 2.0 + 0.5
    y_crossings = _collect_y_crossings(x, segments)

    if len(y_crossings) < 2:
        return False

    y_crossings.sort()

    main_coverage = 0.0
    accent_coverage = 0.0

    for i in range(0, len(y_crossings) - 1, 2):
        y_low = y_crossings[i]
        y_high = y_crossings[i + 1]

        # Below baseline → accent zone (cedilla, ogonek)
        if y_low < 0:
            accent_coverage += min(y_high, 0) - y_low

        # Above reference → accent zone (dieresis, acute)
        if y_high > reference_y:
            accent_coverage += y_high - max(y_low, reference_y)

        # Main body zone [0, reference_y]
        zone_low = max(y_low, 0)
        zone_high = min(y_high, reference_y)
        if zone_high > zone_low:
            main_coverage += zone_high - zone_low

    total = main_coverage + accent_coverage
    if total <= 0:
        return False

    return accent_coverage / total > 0.7


def _filter_small_element_vstems(
    vstems: list[PSHint],
    coverage_map: dict[str, float],
    glyph,
) -> list[PSHint]:
    """Filter vstems belonging to small glyph elements (legs, tails, accents).

    For alphabetic glyphs (Lu/Ll/Lt):

    1. Composite glyphs: decompose only the base component (tallest by bounds).
       vstems with zero coverage on the base contours are accent vstems.

    2. Non-composite glyphs: vstems with filled height < 40% of max are
       candidates for removal. A candidate is removed if:
       a) It is an accent stem — coverage predominantly (>70%) outside
          the main body zone [0, capHeight] for uppercase or [0, xHeight]
          for lowercase. Catches dieresis dots, cedilla hooks, etc.
       b) It touches or overlaps (within 3-unit tolerance) a main stem —
          catches legs of Д/Ц that adjoin the main vertical stroke.
       Otherwise the candidate is kept (e.g. bowl stems in Ь/Б/Ъ).

    Non-alphabetic glyphs or glyphs without unicode are returned unchanged.
    """
    if not vstems or len(vstems) < 2:
        return vstems

    if not _is_alphabetic_glyph(glyph):
        return vstems

    # Composite glyph: filter by base component coverage
    if not glyph.contours and glyph.components:
        return _filter_accent_vstems_composite(vstems, glyph)

    # Non-composite: filter by relative coverage threshold
    coverages = [coverage_map.get(s.raw, 0.0) for s in vstems]
    max_cov = max(coverages)

    if max_cov <= 0:
        return vstems

    threshold = max_cov * 0.4
    main = [s for s, c in zip(vstems, coverages) if c >= threshold]
    candidates = [s for s, c in zip(vstems, coverages) if c < threshold]

    if not candidates or len(main) < 1:
        return vstems

    # Extract segments for accent zone detection
    reference_y = _get_accent_reference_y(glyph)
    segments = None
    if reference_y is not None:
        try:
            segments = _extract_segments(glyph)
        except Exception:
            pass

    TOUCH_TOLERANCE = 3.0
    removed = []
    for cand in candidates:
        # Check 1: accent zone — coverage mostly outside main body [0, ref_y]
        if segments and reference_y is not None:
            if _is_accent_zone_stem(cand, segments, reference_y):
                removed.append(cand)
                continue

        # Check 2: touches or overlaps a main stem (±3 units)
        touches_main = False
        for m in main:
            if (cand.position - TOUCH_TOLERANCE) < m.end and (
                m.position - TOUCH_TOLERANCE
            ) < cand.end:
                touches_main = True
                break
        if touches_main:
            removed.append(cand)
        else:
            main.append(cand)

    if removed:
        logger.debug(f"Filtered small-element vstems: {[s.raw for s in removed]}")
    return main


def _filter_accent_vstems_composite(
    vstems: list[PSHint],
    glyph,
) -> list[PSHint]:
    """Filter accent vstems from a composite glyph.

    Compares the composite's vstems against the base component's own
    vstems (from its processed layer hints). vstems that don't match
    any base vstem are accent vstems and get filtered.

    Match tolerance: position ±3, width ±5 units.
    """
    font = glyph.font
    if font is None:
        return vstems

    # Find base component (tallest by bounding box height)
    base_comp = None
    base_height = -1.0

    for comp in glyph.components:
        base_name = comp.baseGlyph
        if base_name not in font:
            continue
        base_glyph = font[base_name]
        bounds = base_glyph.bounds
        if bounds is None:
            continue
        h = bounds[3] - bounds[1]
        if h > base_height:
            base_height = h
            base_comp = comp

    if base_comp is None:
        return vstems

    # Get base glyph's own vstems from its hint data
    from ufo_tdkit_tools.ps_hints.parser import HintSource, parse_ps_hints

    base_glyph = font[base_comp.baseGlyph]
    base_hd = parse_ps_hints(base_glyph, HintSource.PROCESSED_LAYER, font=font)

    if base_hd is None or not base_hd.hint_sets:
        # Base has no hints → all vstems are accent → remove all
        return []

    base_vstems = _collect_unique_stems(base_hd, vertical=True)
    base_vstems = [s for s in base_vstems if not s.is_ghost and not s.is_triple]

    if not base_vstems:
        # Base has no vstems → all are accent → remove all
        return []

    # Apply base component transformation offset to base vstem positions
    _, _, _, _, dx, _ = base_comp.transformation
    base_positions = [(s.position + dx, s.width) for s in base_vstems]

    # Match composite vstems against base vstems
    pos_tol = 3.0
    width_tol = 5.0

    def matches_base(stem: PSHint) -> bool:
        for bp, bw in base_positions:
            if abs(stem.position - bp) <= pos_tol and abs(stem.width - bw) <= width_tol:
                return True
        return False

    main = [s for s in vstems if matches_base(s)]
    small = [s for s in vstems if not matches_base(s)]

    if small:
        removed = [s.raw for s in small]
        logger.debug(f"Filtered accent vstems (composite): {removed}")

    return main


def _extract_segments_from_contours(contours) -> list[list[tuple[float, float]]]:
    """Extract segments from a contour list (shared logic)."""
    segments: list[list[tuple[float, float]]] = []

    for contour in contours:
        points = contour.points
        if not points:
            continue

        last_oncurve: tuple[float, float] | None = None
        for p in reversed(points):
            if p.type != "offcurve":
                last_oncurve = (p.x, p.y)
                break

        if last_oncurve is None:
            continue

        prev = last_oncurve
        offcurves: list[tuple[float, float]] = []

        for p in points:
            if p.type == "offcurve":
                offcurves.append((p.x, p.y))
            else:
                pt = (p.x, p.y)
                if not offcurves:
                    if prev != pt:
                        segments.append([prev, pt])
                elif len(offcurves) == 1:
                    segments.append([prev, offcurves[0], pt])
                elif len(offcurves) == 2:
                    segments.append([prev, offcurves[0], offcurves[1], pt])
                else:
                    for i in range(len(offcurves)):
                        if i == len(offcurves) - 1:
                            segments.append([prev, offcurves[i], pt])
                        else:
                            oc_a = offcurves[i]
                            oc_b = offcurves[i + 1]
                            implied = ((oc_a[0] + oc_b[0]) / 2.0, (oc_a[1] + oc_b[1]) / 2.0)
                            segments.append([prev, oc_a, implied])
                            prev = implied
                prev = pt
                offcurves = []

    return segments


def _apply_component_transform(
    segments: list[list[tuple[float, float]]],
    component,
) -> list[list[tuple[float, float]]]:
    """Apply a component's transformation to segment coordinates."""
    transformation = component.transformation
    if transformation == (1, 0, 0, 1, 0, 0):
        return segments  # identity

    xx, xy, yx, yy, dx, dy = transformation
    result: list[list[tuple[float, float]]] = []
    for seg in segments:
        new_seg = []
        for x, y in seg:
            nx = xx * x + yx * y + dx
            ny = xy * x + yy * y + dy
            new_seg.append((nx, ny))
        result.append(new_seg)
    return result


def _is_alphabetic_glyph(glyph) -> bool:
    """Check if glyph is an alphabetic character (Lu, Ll, Lt) by unicode."""
    import unicodedata

    uni = getattr(glyph, "unicode", None)
    if uni is None:
        # Try unicodes list
        unis = getattr(glyph, "unicodes", None)
        if unis:
            uni = unis[0]
    if uni is None:
        return False

    try:
        category = unicodedata.category(chr(uni))
        return category in ("Lu", "Ll", "Lt")
    except (ValueError, TypeError):
        return False


# ── Horizontal stem optimization ─────────────────────────────────────────────


def _optimize_hstems(
    hstems: list[PSHint],
    stem_snap_h: list[float] | None,
) -> list[PSHint]:
    """Resolve overlapping hstems by keeping the one closest to stemSnap."""
    if len(hstems) <= 1:
        return hstems

    hstems = sorted(hstems, key=lambda s: s.position)

    groups: list[list[PSHint]] = []
    current_group = [hstems[0]]

    for s in hstems[1:]:
        if any(_stems_overlap(s, g) for g in current_group):
            current_group.append(s)
        else:
            groups.append(current_group)
            current_group = [s]
    groups.append(current_group)

    result: list[PSHint] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
        else:
            result.append(_pick_best_stem(group, stem_snap_h))

    return result


# ── Vertical stem optimization ───────────────────────────────────────────────


def _optimize_vstems(
    vstems: list[PSHint],
    glyph_width: float,
    stem_snap_v: list[float] | None,
    upm: int,
    coverage_map: dict[str, float] | None = None,
) -> list[PSHint]:
    """Optimize vertical stems: resolve overlaps.

    Note: too-wide filtering and vstem3 extraction happen earlier in the
    pipeline (in optimize_hints), so this function only resolves overlaps.
    """
    if not vstems:
        return vstems

    # Classify L/R by glyph center and resolve overlaps
    center = glyph_width / 2.0
    left = [s for s in vstems if (s.position + s.width / 2.0) < center]
    right = [s for s in vstems if (s.position + s.width / 2.0) >= center]

    # Check for cross-side overlaps: if any left stem overlaps any right stem,
    # treat all as centric (e.g. glyph "i" where dot and stroke overlap)
    has_cross_overlap = any(_stems_overlap(ls, rs) for ls in left for rs in right)

    if has_cross_overlap:
        vstems = _resolve_centric_overlaps(vstems, stem_snap_v, coverage_map)
    else:
        left = _resolve_overlaps_left(left, stem_snap_v, coverage_map)
        right = _resolve_overlaps_right(right, stem_snap_v, coverage_map)
        vstems = left + right

    return vstems


def _snap_tolerance(snap_value: float) -> float:
    """Acceptable deviation from a single stemSnap value.

    Tolerance scales as 20% of the snap value with a 5-unit floor so that
    Thin masters (snap ~20) get a usable ±5 window while Black/Display
    masters (snap 250-400) get proportionally larger ±50-80 windows.
    """
    return max(5.0, snap_value * 0.20)


def _is_snap_compatible(width: float, snaps: list[float]) -> bool:
    """True if `width` is within tolerance of at least one snap value."""
    return any(abs(width - s) <= _snap_tolerance(s) for s in snaps)


def _is_centric(vstems: list[PSHint], center: float) -> bool:
    """Check if all stems are clustered near the center (like i, j, l)."""
    if len(vstems) < 2:
        return False
    spread = max(s.position + s.width for s in vstems) - min(s.position for s in vstems)
    return spread < center * 0.8


def _stems_overlap(a: PSHint, b: PSHint) -> bool:
    """Check if two stems overlap in their ranges (strict, excludes touching)."""
    return a.position < b.end and b.position < a.end


def _stems_touch_or_overlap(a: PSHint, b: PSHint) -> bool:
    """Check if two stems overlap or touch (share a boundary)."""
    return a.position <= b.end and b.position <= a.end


def _resolve_overlaps_left(
    stems: list[PSHint],
    stem_snap_v: list[float] | None = None,
    coverage_map: dict[str, float] | None = None,
) -> list[PSHint]:
    """For left-side stems: among overlapping groups, keep the leftmost."""
    if len(stems) <= 1:
        return stems
    stems = sorted(stems, key=lambda s: s.position)
    return _resolve_overlap_groups(stems, keep="leftmost", stem_snap=stem_snap_v,
                                   coverage_map=coverage_map)


def _resolve_overlaps_right(
    stems: list[PSHint],
    stem_snap_v: list[float] | None = None,
    coverage_map: dict[str, float] | None = None,
) -> list[PSHint]:
    """For right-side stems: among overlapping groups, keep the rightmost."""
    if len(stems) <= 1:
        return stems
    stems = sorted(stems, key=lambda s: s.position)
    return _resolve_overlap_groups(stems, keep="rightmost", stem_snap=stem_snap_v,
                                   coverage_map=coverage_map)


def _has_mixed_widths(group: list[PSHint]) -> bool:
    """Check if overlap group has significantly different widths (stem vs dot)."""
    widths = [s.width for s in group]
    max_w = max(widths)
    min_w = min(widths)
    return (max_w - min_w) > max(max_w * 0.15, 10.0)


def _has_different_snap_distances(
    group: list[PSHint],
    stem_snap: list[float],
) -> bool:
    """Check if stems in a group have different stemSnap proximity.

    Returns True when the best and worst snap distances differ enough
    to make snap-based selection meaningful (e.g., i: width 80 snap=0
    vs width 90 snap=4).
    """
    distances = [_snap_distance(s.width, stem_snap) for s in group]
    return max(distances) - min(distances) > 0.5


def _has_significant_coverage_difference(
    group: list[PSHint],
    coverage_map: dict[str, float],
) -> bool:
    """Check if stems in a group have very different height coverage.

    Returns True if the tallest stem covers at least 2x more than the
    shortest, with at least 100 units absolute difference. This catches
    cases like Cyrillic Щ/Ц where a main stem overlaps with a small
    descending tail.
    """
    coverages = [coverage_map.get(s.raw, 0.0) for s in group]
    if not any(c > 0 for c in coverages):
        return False
    max_c = max(coverages)
    min_c = min(coverages)
    return max_c > 0 and (max_c - min_c) > 100 and max_c > min_c * 2.0


def _resolve_overlap_groups(
    stems: list[PSHint],
    keep: str,
    stem_snap: list[float] | None = None,
    coverage_map: dict[str, float] | None = None,
) -> list[PSHint]:
    """Group overlapping stems and keep one per group.

    Resolution priority:
    1. Significant coverage difference (>2x) -> pick by combined score
    2. Mixed widths (stem vs dot) -> pick by stemSnap proximity
    3. Position-based (leftmost/rightmost)
    """
    if not stems:
        return stems

    groups: list[list[PSHint]] = []
    current_group = [stems[0]]

    for s in stems[1:]:
        if any(_stems_overlap(s, g) for g in current_group):
            current_group.append(s)
        else:
            groups.append(current_group)
            current_group = [s]
    groups.append(current_group)

    result: list[PSHint] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
        elif coverage_map and _has_significant_coverage_difference(group, coverage_map):
            # Coverage dominates: main stem vs small element (e.g. Щ tail)
            result.append(_pick_best_stem(group, stem_snap, coverage_map))
        elif stem_snap and _has_different_snap_distances(group, stem_snap):
            # Different stemSnap proximity: prefer closer to snap (e.g. i/j)
            result.append(_pick_best_stem(group, stem_snap, coverage_map))
        elif stem_snap and _has_mixed_widths(group):
            # Mixed widths: stem vs dot -- pick closest to stemSnap
            result.append(_pick_best_stem(group, stem_snap, coverage_map))
        elif keep == "leftmost":
            result.append(min(group, key=lambda s: s.position))
        else:  # rightmost
            result.append(max(group, key=lambda s: s.end))

    return result


def _resolve_centric_overlaps(
    vstems: list[PSHint],
    stem_snap_v: list[float] | None,
    coverage_map: dict[str, float] | None = None,
) -> list[PSHint]:
    """For centric glyphs: among overlapping stems, keep the best one."""
    if len(vstems) <= 1:
        return vstems

    vstems = sorted(vstems, key=lambda s: s.position)
    groups: list[list[PSHint]] = []
    current_group = [vstems[0]]

    for s in vstems[1:]:
        if any(_stems_overlap(s, g) for g in current_group):
            current_group.append(s)
        else:
            groups.append(current_group)
            current_group = [s]
    groups.append(current_group)

    result: list[PSHint] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
        else:
            result.append(_pick_best_stem(group, stem_snap_v, coverage_map))

    return result


def _pick_best_stem(
    stems: list[PSHint],
    stem_snap: list[float] | None,
    coverage_map: dict[str, float] | None = None,
) -> PSHint:
    """Pick the best stem from a group of overlapping stems.

    Combined scoring when both stemSnap and coverage are available:
    - snap_distance: lower = closer to stemSnap = better
    - coverage: higher = more contour coverage = better

    The coverage bonus is normalized so that full coverage equals
    ~5 font units of snap advantage. This means:
    - snap difference > 10u: snap always wins
    - snap difference < 5u: coverage breaks the tie
    """
    if not stem_snap and not coverage_map:
        return min(stems, key=lambda s: s.width)

    if not coverage_map:
        # Snap-only (original behavior)
        if stem_snap:
            return min(stems, key=lambda s: _snap_distance(s.width, stem_snap))
        return min(stems, key=lambda s: s.width)

    # Combined scoring
    coverages = [coverage_map.get(s.raw, 0.0) for s in stems]
    max_coverage = max(coverages) if coverages else 0.0

    def score(s: PSHint) -> float:
        snap_dist = _snap_distance(s.width, stem_snap) if stem_snap else 0.0
        coverage = coverage_map.get(s.raw, 0.0)
        # Normalize coverage to [0, 1]
        norm_cov = coverage / max_coverage if max_coverage > 0 else 0.0
        # Lower score = better. Coverage bonus up to 5 units equivalent.
        return snap_dist - norm_cov * 5.0

    return min(stems, key=score)


def _snap_distance(width: float, snap_values: list[float]) -> float:
    """Distance from width to nearest snap value."""
    return min(abs(width - sv) for sv in snap_values)


# ── Apply optimized hints back to processed layer ────────────────────────────


def apply_optimized_hints(glyph, font, hint_data: PSHintData) -> bool:
    """Write optimized hints back to the processed layer glyph.

    Args:
        glyph: fontParts glyph from the default layer.
        font: fontParts font object.
        hint_data: Optimized PSHintData (single hint set).

    Returns:
        True if applied successfully.
    """
    try:
        layer_names = [layer.name for layer in font.layers]
        if PROCESSED_LAYER_NAME not in layer_names:
            return False

        processed_layer = font.getLayer(PROCESSED_LAYER_NAME)
        if glyph.name not in processed_layer:
            return False

        processed_glyph = processed_layer[glyph.name]
    except Exception as e:
        logger.error(f"Cannot access processed glyph '{glyph.name}': {e}")
        return False

    # Build hint dict from optimized data
    hint_dict = _hint_data_to_dict(hint_data, processed_glyph)
    processed_glyph.lib[ADOBE_HINT_KEY_V2] = hint_dict

    logger.info(f"Applied optimized hints for '{glyph.name}'")
    return True


def _hint_data_to_dict(hint_data: PSHintData, processed_glyph) -> dict[str, Any]:
    """Convert PSHintData back to the autohint v2 dict format."""
    hint_set_list = []
    for hs in hint_data.hint_sets:
        hs_dict: dict[str, Any] = {}
        if hs.point_tag:
            hs_dict["pointTag"] = hs.point_tag
        hs_dict["stems"] = [s.raw for s in hs.stems]
        hint_set_list.append(hs_dict)

    result: dict[str, Any] = {}
    if hint_data.format_version:
        result["formatVersion"] = hint_data.format_version

    # For a single optimized set, assign pointTag to first oncurve point
    if hint_set_list and not hint_set_list[0].get("pointTag"):
        first_tag = _get_first_oncurve_name(processed_glyph)
        if first_tag:
            hint_set_list[0]["pointTag"] = first_tag

    result["hintSetList"] = hint_set_list

    if hint_data.flex_points:
        result["flexList"] = list(hint_data.flex_points)

    result["id"] = compute_outline_hash(processed_glyph)

    return result


def _get_first_oncurve_name(glyph) -> str | None:
    """Get or assign a name to the first oncurve point."""
    for contour in glyph.contours:
        for point in contour.points:
            if point.type != "offcurve":
                if point.name:
                    return point.name
                # Assign a hintRef name
                existing = set()
                for c in glyph.contours:
                    for p in c.points:
                        if p.name:
                            existing.add(p.name)
                counter = 0
                while True:
                    candidate = f"hintRef{counter:04d}"
                    if candidate not in existing:
                        point.name = candidate
                        return candidate
                    counter += 1
    return None
