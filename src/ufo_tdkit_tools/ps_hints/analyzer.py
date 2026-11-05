# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
PostScript hints analyzer.

Detects optimization opportunities in PS hint data:

1. Snap-incompatible vstems (width not within ~20% of any stemSnap value)
2. Overlapping vstems (same-side and cross-side/centric)
3. Overlapping hstems
4. Potential vstem3 candidates (3 equally-spaced similar-width vstems)
5. Overlapping hints within one hint set, ghosts included

Checks 1-4 pool the stems of all hint sets, as the optimizer does when it
flattens a glyph to one clean set: their overlaps may sit in different sets,
which is legal hint replacement. Check 5 looks at one set at a time, where an
overlap is an error (Type 2 Charstring Format, #5177 section 4.3: overlapping
hints in one active set give undefined results).
"""

from __future__ import annotations

import logging
from itertools import combinations

from ufo_tdkit_tools.ps_hints.parser import PSHintData
from ufo_tdkit_tools.ps_hints.optimizer import (
    _collect_unique_stems,
    _has_mixed_widths,
    _is_snap_compatible,
    _pick_best_stem,
    _snap_distance,
    _stems_overlap,
)

logger = logging.getLogger(__name__)


def analyze_glyph(
    hint_data: PSHintData,
    glyph_width: float,
    stem_snap_v: list[float] | None,
    stem_snap_h: list[float] | None,
    upm: int,
) -> list[tuple[str, str]] | None:
    """Analyze hint data for optimization opportunities.

    Returns list of (pattern_tag, detail_string) or None if no issues.

    Pattern tags:
        snap-incompatible-v: vstem width not within ~20% (floor 5u) of any
            stemSnap value
        centric-overlap: cross-side vstem overlap (narrow glyphs like i, j)
        overlap-v: same-side vstem overlap
        overlap-h: hstem overlap
        potential-vstem3: 3 vstems matching Adobe stem3 constraints
        overlap-set-v: two vstems of the same hint set overlap
        overlap-set-h: two hstems of the same hint set overlap

    The centric-overlap / overlap-v / overlap-h checks pool the stems of all
    hint sets, so their pairs may come from different sets (legal hint
    replacement, but the optimizer keeps only one). overlap-set-v / overlap-set-h
    compare the stems of one set only, ghosts included with their single edge
    as the extent; such a pair is invalid in the charstring.
    """
    if not hint_data.hint_sets:
        return None

    issues: list[tuple[str, str]] = []

    # Collect unique stems
    all_vstems = _collect_unique_stems(hint_data, vertical=True)
    all_hstems = _collect_unique_stems(hint_data, vertical=False)

    regular_vstems = [s for s in all_vstems if not s.is_ghost and not s.is_triple]
    regular_hstems = [s for s in all_hstems if not s.is_ghost and not s.is_triple]

    # Check snap-incompatible vstems
    if regular_vstems and stem_snap_v:
        for s in regular_vstems:
            if not _is_snap_compatible(s.width, stem_snap_v):
                nearest = min(stem_snap_v, key=lambda v: abs(s.width - v))
                issues.append(
                    (
                        "snap-incompatible-v",
                        f"vstem {_fmt(s.position)}+{_fmt(s.width)} "
                        f"(w={_fmt(s.width)}, nearest snap={_fmt(nearest)})",
                    )
                )

    # Check vstem overlaps (operate on the snap-compatible subset)
    if regular_vstems and stem_snap_v:
        non_wide = [s for s in regular_vstems if _is_snap_compatible(s.width, stem_snap_v)]
    elif regular_vstems:
        max_w = upm * 0.3
        non_wide = [s for s in regular_vstems if s.width <= max_w]
    else:
        non_wide = []
    if len(non_wide) >= 2:
        center = glyph_width / 2.0
        left = [s for s in non_wide if (s.position + s.width / 2.0) < center]
        right = [s for s in non_wide if (s.position + s.width / 2.0) >= center]

        # Cross-side overlaps (centric)
        for ls in left:
            for rs in right:
                if _stems_overlap(ls, rs):
                    best = _pick_best_stem([ls, rs], stem_snap_v)
                    other = rs if best is ls else ls
                    snap_info = ""
                    if stem_snap_v:
                        snap_info = f" (≈{_fmt(stem_snap_v[0])})"
                    issues.append(
                        (
                            "centric-overlap",
                            f"vstem {_fmt(other.position)}+{_fmt(other.width)} / "
                            f"{_fmt(best.position)}+{_fmt(best.width)} → "
                            f"keep {_fmt(best.position)}+{_fmt(best.width)}{snap_info}",
                        )
                    )

        # Same-side overlaps
        for side_name, side_stems in [("L", left), ("R", right)]:
            if len(side_stems) < 2:
                continue
            sorted_stems = sorted(side_stems, key=lambda s: s.position)
            for i in range(len(sorted_stems)):
                for j in range(i + 1, len(sorted_stems)):
                    if _stems_overlap(sorted_stems[i], sorted_stems[j]):
                        a, b = sorted_stems[i], sorted_stems[j]
                        if stem_snap_v and _has_mixed_widths([a, b]):
                            keep = _pick_best_stem([a, b], stem_snap_v)
                        elif side_name == "L":
                            keep = a if a.position <= b.position else b
                        else:
                            keep = a if a.end >= b.end else b
                        drop = b if keep is a else a
                        snap_info = ""
                        if stem_snap_v:
                            snap_info = f" (≈{_fmt(stem_snap_v[0])})"
                        issues.append(
                            (
                                "overlap-v",
                                f"vstem {_fmt(drop.position)}+{_fmt(drop.width)} / "
                                f"{_fmt(keep.position)}+{_fmt(keep.width)} → "
                                f"keep {_fmt(keep.position)}+{_fmt(keep.width)}{snap_info}",
                            )
                        )

    # Check potential vstem3 (C(N,3) combinatorial search)
    if len(non_wide) >= 3:
        best_triple = _find_vstem3_candidate(non_wide, stem_snap_v)
        if best_triple is not None:
            s0, s1, s2 = best_triple
            issues.append(
                (
                    "potential-vstem3",
                    f"3 vstems: {_fmt(s0.position)}+{_fmt(s0.width)}, "
                    f"{_fmt(s1.position)}+{_fmt(s1.width)}, "
                    f"{_fmt(s2.position)}+{_fmt(s2.width)} → vstem3",
                )
            )

    # Check hstem overlaps
    if len(regular_hstems) >= 2:
        sorted_h = sorted(regular_hstems, key=lambda s: s.position)
        for i in range(len(sorted_h)):
            for j in range(i + 1, len(sorted_h)):
                if _stems_overlap(sorted_h[i], sorted_h[j]):
                    a, b = sorted_h[i], sorted_h[j]
                    best = _pick_best_stem([a, b], stem_snap_h)
                    other = b if best is a else a
                    snap_info = ""
                    if stem_snap_h:
                        snap_info = f" (≈{_fmt(stem_snap_h[0])})"
                    issues.append(
                        (
                            "overlap-h",
                            f"hstem {_fmt(other.position)}+{_fmt(other.width)} / "
                            f"{_fmt(best.position)}+{_fmt(best.width)} → "
                            f"keep {_fmt(best.position)}+{_fmt(best.width)}{snap_info}",
                        )
                    )

    issues.extend(_set_overlaps(hint_data))

    return issues if issues else None


def _hint_extents(stem) -> list[tuple[float, float]]:
    """Return the (low, high) edge ranges a hint controls.

    A ghost controls a single edge: the top/right ghost (width -20) sits at its
    position, the bottom/left ghost (width -21) at position - 21. A triple
    contributes its three stems.
    """
    if stem.is_triple and stem.pairs:
        pairs = stem.pairs
    else:
        pairs = [(stem.position, stem.width)]
    out = []
    for pos, width in pairs:
        if width == -20:
            out.append((pos, pos))
        elif width == -21:
            out.append((pos - 21, pos - 21))
        else:
            out.append((min(pos, pos + width), max(pos, pos + width)))
    return out


def _set_overlaps(hint_data: PSHintData) -> list[tuple[str, str]]:
    """Report hints that overlap within one hint set (strict, touching is fine)."""
    issues: list[tuple[str, str]] = []
    for hs in hint_data.hint_sets:
        label = f"set {hs.index}" + (f" ({hs.point_tag})" if hs.point_tag else "")
        for vertical, tag in ((True, "overlap-set-v"), (False, "overlap-set-h")):
            seen: set[str] = set()
            stems = []
            for s in hs.stems:
                if s.is_vertical == vertical and s.raw not in seen:
                    seen.add(s.raw)
                    stems.append(s)
            for a, b in combinations(stems, 2):
                if any(
                    lo_a < hi_b and lo_b < hi_a
                    for lo_a, hi_a in _hint_extents(a)
                    for lo_b, hi_b in _hint_extents(b)
                ):
                    a, b = sorted((a, b), key=lambda s: _hint_extents(s)[0][0])
                    issues.append((tag, f"{label}: {_fmt_hint(a)} / {_fmt_hint(b)}"))
    return issues


def _fmt_hint(stem) -> str:
    """Format a hint as in autohint.v2, e.g. 'hstem 700 -20'."""
    if stem.is_triple and stem.pairs:
        return f"{stem.type} " + " ".join(f"{_fmt(p)} {_fmt(w)}" for p, w in stem.pairs)
    return f"{stem.type} {_fmt(stem.position)} {_fmt(stem.width)}"


def _find_vstem3_candidate(
    vstems: list,
    stem_snap_v: list[float] | None,
) -> tuple | None:
    """Find the best vstem3 candidate among all C(N,3) combinations.

    Returns (s0, s1, s2) sorted by position, or None if no valid triple.
    """
    from ufo_tdkit_tools.ps_hints.parser import PSHint

    best: tuple[PSHint, ...] | None = None
    best_score = float("inf")

    for combo in combinations(vstems, 3):
        s0, s1, s2 = sorted(combo, key=lambda s: s.position)

        # No pairwise overlaps
        if _stems_overlap(s0, s1) or _stems_overlap(s1, s2) or _stems_overlap(s0, s2):
            continue

        # Outer widths equal
        if abs(s0.width - s2.width) > 1.0:
            continue

        # Center-to-center equal spacing
        c0 = s0.position + s0.width / 2.0
        c1 = s1.position + s1.width / 2.0
        c2 = s2.position + s2.width / 2.0
        if abs((c1 - c0) - (c2 - c1)) > 1.0:
            continue

        # Valid — score by stemSnap proximity
        if stem_snap_v:
            score = sum(_snap_distance(s.width, stem_snap_v) for s in (s0, s1, s2))
        else:
            score = 0.0

        if best is None or score < best_score:
            best = (s0, s1, s2)
            best_score = score

    return best


def _fmt(v: float) -> str:
    """Format float: show as int if whole, otherwise 1 decimal."""
    return str(int(v)) if v == int(v) else f"{v:.1f}"
