# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
CFF charstring hint extractor for binary -> UFO conversion.

Extracts per-glyph PostScript hints from CFF (Type 2) charstrings
and writes them to UFO glyph lib in com.adobe.type.autohint.v2 format.

This fills a gap: no existing Python tool extracts per-glyph PS hints
from compiled CFF fonts into UFO format. The data IS in the charstrings
(hstem/vstem/hintmask operators), but nobody connected it to the UFO
hint storage spec until now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from fontTools.misc.psCharStrings import SimpleT2Decompiler
from fontTools.ttLib import TTFont

from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2, compute_outline_hash
from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

logger = logging.getLogger(__name__)

# Coordinate matching tolerance (font units)
_COORD_TOLERANCE = 0.5


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class HintMaskInfo:
    """Records a hintmask/cntrmask encountered during charstring execution."""

    mask_bytes: bytes
    position: tuple[float, float]  # (x, y) at the point where mask was applied
    num_hstems: int
    num_vstems: int


@dataclass
class GlyphHintResult:
    """Extracted hint data for a single glyph."""

    hstems: list[tuple[float, float]] = field(default_factory=list)  # (pos, width)
    vstems: list[tuple[float, float]] = field(default_factory=list)
    hint_masks: list[HintMaskInfo] = field(default_factory=list)
    drawing_points: list[tuple[float, float]] = field(default_factory=list)
    has_hints: bool = False


# ── T2 Charstring decompiler with hint extraction ────────────────────────────


class HintExtractingDecompiler(SimpleT2Decompiler):
    """T2 CharString decompiler that intercepts hint operators.

    Collects horizontal and vertical stem hints with proper handling of:
    - Cumulative delta encoding (position accumulates across stem pairs)
    - Optional width value (first stack-clearing op may have odd arg count)
    - Implicit vstems before hintmask/cntrmask
    """

    def __init__(self, localSubrs, globalSubrs):
        super().__init__(localSubrs, globalSubrs)
        self.hstems: list[tuple[float, float]] = []
        self.vstems: list[tuple[float, float]] = []
        self._width_seen = False  # Track if optional width was consumed
        # Cumulative position tracking across multiple operator calls
        self._h_pos = 0.0
        self._v_pos = 0.0

    def _pop_and_strip_width(self) -> list:
        """Pop all args, stripping optional width value from first operator.

        In T2 charstrings, the first stack-clearing operator may have an
        extra arg (odd count) which is the glyph width, not a hint value.
        """
        args = self.popall()
        if not self._width_seen:
            self._width_seen = True
            if len(args) % 2 == 1:
                args = args[1:]  # Strip width value
        return args

    def _collect_stems(self, args: list, target: list, vertical: bool) -> None:
        """Decode cumulative stem encoding from CFF charstring.

        CFF stems use cumulative deltas within and across operator calls:
        hstem -10 70 -39 -21  ->  stem at -10 (w=70), then at 21 (w=-21 ghost)
        """
        pos = self._v_pos if vertical else self._h_pos
        for i in range(0, len(args) - 1, 2):
            delta = args[i]
            width = args[i + 1]
            pos += delta
            target.append((pos, width))
            pos += width
        if vertical:
            self._v_pos = pos
        else:
            self._h_pos = pos

    def op_hstem(self, index):
        args = self._pop_and_strip_width()
        self.hintCount += len(args) // 2
        self._collect_stems(args, self.hstems, vertical=False)

    def op_vstem(self, index):
        args = self._pop_and_strip_width()
        self.hintCount += len(args) // 2
        self._collect_stems(args, self.vstems, vertical=True)

    def op_hstemhm(self, index):
        args = self._pop_and_strip_width()
        self.hintCount += len(args) // 2
        self._collect_stems(args, self.hstems, vertical=False)

    def op_vstemhm(self, index):
        args = self._pop_and_strip_width()
        self.hintCount += len(args) // 2
        self._collect_stems(args, self.vstems, vertical=True)

    def op_hintmask(self, index):
        # Any remaining stack args are implicit vstems
        if self.operandStack:
            args = self._pop_and_strip_width()
            self.hintCount += len(args) // 2
            self._collect_stems(args, self.vstems, vertical=True)
        # Let parent handle the mask bytes (reads mask bytes, advances index)
        return super().op_hintmask(index)

    def op_cntrmask(self, index):
        if self.operandStack:
            args = self._pop_and_strip_width()
            self.hintCount += len(args) // 2
            self._collect_stems(args, self.vstems, vertical=True)
        return super().op_cntrmask(index)


def _execute_charstring(charstring, local_subrs, global_subrs) -> GlyphHintResult:
    """Execute a T2 charstring and extract hint data.

    Uses HintExtractingDecompiler (subclass of SimpleT2Decompiler) to
    follow subroutine calls and collect all stem hints.

    Args:
        charstring: Decompiled T2CharString.
        local_subrs: Local subroutines from CFF Private dict.
        global_subrs: Global subroutines from CFF table.
    """
    result = GlyphHintResult()

    try:
        decompiler = HintExtractingDecompiler(local_subrs, global_subrs)
        decompiler.execute(charstring)
        result.hstems = decompiler.hstems
        result.vstems = decompiler.vstems
    except Exception as e:
        logger.debug(f"Decompiler failed: {e}")
        return result

    if not result.hstems and not result.vstems:
        return result

    result.has_hints = True
    return result


# ── Stem formatting ──────────────────────────────────────────────────────────


def _format_stem(stem_type: str, pos: float, width: float) -> str:
    """Format a stem as 'hstem <pos> <width>' string."""

    def _fmt(v: float) -> str:
        if v == int(v):
            return str(int(v))
        return f"{v:g}"

    return f"{stem_type} {_fmt(pos)} {_fmt(width)}"


# ── Point naming ─────────────────────────────────────────────────────────────


def _get_first_oncurve_name(ufo_glyph, used_names: set) -> str | None:
    """Get or assign a name to the first on-curve point."""
    for contour in ufo_glyph:
        for point in contour.points:
            if point.type != "offcurve":
                if point.name:
                    used_names.add(point.name)
                    return point.name
                counter = 0
                while True:
                    candidate = f"hintRef{counter:04d}"
                    if candidate not in used_names:
                        point.name = candidate
                        used_names.add(candidate)
                        return candidate
                    counter += 1
    return None


# ── Build UFO hint dict ──────────────────────────────────────────────────────


def _build_hint_dict(
    glyph_name: str,
    hint_result: GlyphHintResult,
    ufo_glyph,
    outline_hash: str,
) -> dict[str, Any] | None:
    """Build com.adobe.type.autohint.v2 dict from extracted CFF hints.

    Creates a single hint set with all stems. Hint substitution (multiple
    hint sets with hintmask-based selection) is not yet supported -- all
    stem data is preserved in one set.
    """
    if not hint_result.has_hints:
        return None

    used_names: set[str] = set()
    for contour in ufo_glyph:
        for point in contour.points:
            if point.name:
                used_names.add(point.name)

    # Build single hint set with all stems
    all_stems = []
    for pos, width in hint_result.hstems:
        all_stems.append(_format_stem("hstem", pos, width))
    for pos, width in hint_result.vstems:
        all_stems.append(_format_stem("vstem", pos, width))

    if not all_stems:
        return None

    # Assign pointTag to first on-curve point
    point_tag = _get_first_oncurve_name(ufo_glyph, used_names)

    entry: dict[str, Any] = {"stems": all_stems}
    if point_tag:
        entry["pointTag"] = point_tag

    return {
        "formatVersion": "1",
        "id": outline_hash,
        "hintSetList": [entry],
    }


# ── Public API ───────────────────────────────────────────────────────────────


def extract_cff_hints(
    tt_font: TTFont,
    ufo_font,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[int, list[ConversionWarning]]:
    """Extract per-glyph PS hints from CFF charstrings into UFO glyph lib.

    Args:
        tt_font: fontTools TTFont with CFF table.
        ufo_font: fontParts RFont (destination, already has outlines from extractor).
        progress_callback: Optional (current, message) callback.

    Returns:
        (hint_count, warnings) -- number of glyphs with extracted hints and any warnings.
    """
    warnings: list[ConversionWarning] = []

    # Get CFF table
    cff_table = None
    if "CFF " in tt_font:
        cff_table = tt_font["CFF "]
    elif "CFF2" in tt_font:
        warnings.append(
            ConversionWarning(
                category="hints",
                severity=WarningSeverity.WARNING,
                message="CFF2 hint extraction is not supported yet",
            )
        )
        return 0, warnings

    if cff_table is None:
        return 0, warnings

    try:
        cff = cff_table.cff
        top_dict = cff.topDictIndex[0]
        char_strings = top_dict.CharStrings
    except Exception as e:
        warnings.append(
            ConversionWarning(
                category="hints",
                severity=WarningSeverity.ERROR,
                message=f"Failed to access CFF charstrings: {e}",
            )
        )
        return 0, warnings

    # Get subroutines (must be passed explicitly to decompiler)
    private = top_dict.Private
    local_subrs = getattr(private, "Subrs", [])
    global_subrs = getattr(cff, "GlobalSubrs", [])

    glyph_order = list(char_strings.keys())
    hint_count = 0
    error_count = 0

    for idx, glyph_name in enumerate(glyph_order):
        if progress_callback and idx % 100 == 0:
            progress_callback(
                idx,
                f"Extracting PS hints... ({idx}/{len(glyph_order)})",
            )

        # Skip glyphs not in UFO (shouldn't happen, but be safe)
        if glyph_name not in ufo_font:
            continue

        try:
            charstring = char_strings[glyph_name]
            charstring.decompile()
        except Exception:
            continue

        try:
            hint_result = _execute_charstring(charstring, local_subrs, global_subrs)
        except Exception as e:
            error_count += 1
            if error_count <= 5:
                logger.debug(f"Hint extraction failed for '{glyph_name}': {e}")
            continue

        if not hint_result.has_hints:
            continue

        ufo_glyph = ufo_font[glyph_name]

        # Compute outline hash for staleness detection
        try:
            outline_hash = compute_outline_hash(ufo_glyph)
        except Exception:
            outline_hash = ""

        # Build the hint dict
        hint_dict = _build_hint_dict(glyph_name, hint_result, ufo_glyph, outline_hash)
        if hint_dict is None:
            continue

        # Write to glyph lib
        ufo_glyph.lib[ADOBE_HINT_KEY_V2] = hint_dict
        hint_count += 1

    if error_count > 0:
        warnings.append(
            ConversionWarning(
                category="hints",
                severity=WarningSeverity.WARNING,
                message=f"PS hint extraction failed for {error_count} glyphs",
            )
        )

    return hint_count, warnings


def verify_font_level_hints(
    tt_font: TTFont,
    ufo_font,
) -> list[ConversionWarning]:
    """Verify and fill gaps in font-level PS hint data.

    Checks CFF Private dict values against fontinfo fields
    set by extractor, and fills any missing values.
    """
    warnings: list[ConversionWarning] = []

    if "CFF " not in tt_font:
        return warnings

    try:
        cff = tt_font["CFF "].cff
        top_dict = cff.topDictIndex[0]
        private = top_dict.Private
    except Exception:
        return warnings

    info = ufo_font.info

    # Map CFF Private dict -> fontinfo.plist
    _check_and_fill = [
        ("BlueValues", "postscriptBlueValues"),
        ("OtherBlues", "postscriptOtherBlues"),
        ("FamilyBlues", "postscriptFamilyBlues"),
        ("FamilyOtherBlues", "postscriptFamilyOtherBlues"),
        ("StemSnapH", "postscriptStemSnapH"),
        ("StemSnapV", "postscriptStemSnapV"),
    ]

    for cff_key, ufo_key in _check_and_fill:
        cff_val = getattr(private, cff_key, None)
        ufo_val = getattr(info, ufo_key, None)
        if cff_val and not ufo_val:
            setattr(info, ufo_key, list(cff_val))
            warnings.append(
                ConversionWarning(
                    category="info",
                    severity=WarningSeverity.INFO,
                    message=f"Filled missing {ufo_key} from CFF Private dict",
                )
            )

    # Scalar values
    _scalar_check = [
        ("BlueFuzz", "postscriptBlueFuzz"),
        ("BlueShift", "postscriptBlueShift"),
        ("BlueScale", "postscriptBlueScale"),
        ("ForceBold", "postscriptForceBold"),
    ]

    for cff_key, ufo_key in _scalar_check:
        cff_val = getattr(private, cff_key, None)
        ufo_val = getattr(info, ufo_key, None)
        if cff_val is not None and ufo_val is None:
            setattr(info, ufo_key, cff_val)

    return warnings
