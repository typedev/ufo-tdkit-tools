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
from typing import Any, Callable

from fontTools.ttLib import TTFont

from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2, compute_outline_hash
from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

logger = logging.getLogger(__name__)

# ── Stem formatting ──────────────────────────────────────────────────────────


def _format_stem(stem_type: str, pos: float, width: float) -> str:
    """Format a stem as 'hstem <pos> <width>' string."""

    def _fmt(v: float) -> str:
        if v == int(v):
            return str(int(v))
        return f"{v:g}"

    return f"{stem_type} {_fmt(pos)} {_fmt(width)}"


# ── Hint set construction ────────────────────────────────────────────────────


def _format_stem3(stem_type: str, stems_with_pos_width) -> str:
    """Format a counter-triplet entry: ``vstem3 p1 w1 p2 w2 …``.

    AFDKO's `addUfoMask` flattens all selected stems of one orientation
    into a single space-separated string when the glyph has a cntrmask
    affecting that orientation; this matches that encoding.
    """

    def _fmt(v: float) -> str:
        if v == int(v):
            return str(int(v))
        return f"{v:g}"

    parts = [stem_type]
    for pos, width in stems_with_pos_width:
        parts.append(_fmt(pos))
        parts.append(_fmt(width))
    return " ".join(parts)


def _is_counter_orientation(cntr) -> tuple[bool, bool]:
    """Decide whether each orientation should be encoded as a triplet.

    Mirrors AFDKO `addUfoMask`: when the FIRST cntrmask entry has any
    True bit in an orientation, the whole orientation switches to
    `hstem3`/`vstem3` encoding. ``cntr`` is the AFDKO ``glyph.cntr``
    list; absent or empty -> (False, False).
    """
    if not cntr:
        return False, False
    first = cntr[0]
    if not first or len(first) < 2:
        return False, False
    h_mask, v_mask = first[0], first[1]
    h_cntr = bool(h_mask) and any(h_mask)
    v_cntr = bool(v_mask) and any(v_mask)
    return h_cntr, v_cntr


def _decode_active_stems(masks, hstems, vstems, cntr=None) -> list[str]:
    """Convert AFDKO boolean mask pair into UFO hint-string list.

    ``masks`` is ``[hhints, vhints]`` where each is a list of bools the
    same length as the corresponding stem list. Stems whose bool is True
    are emitted as ``hstem``/``vstem`` strings.

    When ``cntr`` (gd.cntr from convertT2ToGlyphData) marks an
    orientation as counter-controlled, all stems of that orientation are
    flattened into a single ``hstem3``/``vstem3`` triplet entry,
    matching AFDKO's `addUfoMask`.
    """
    out: list[str] = []
    h_mask = masks[0] if masks and len(masks) > 0 else None
    v_mask = masks[1] if masks and len(masks) > 1 else None
    h_cntr, v_cntr = _is_counter_orientation(cntr)

    h_pairs = []
    for i, stem in enumerate(hstems):
        if h_mask is None or (i < len(h_mask) and h_mask[i]):
            h_pairs.append(stem.UFOVals())
    if h_cntr and h_pairs:
        out.append(_format_stem3("hstem3", h_pairs))
    else:
        for p, w in h_pairs:
            out.append(_format_stem("hstem", p, w))

    v_pairs = []
    for i, stem in enumerate(vstems):
        if v_mask is None or (i < len(v_mask) and v_mask[i]):
            v_pairs.append(stem.UFOVals())
    if v_cntr and v_pairs:
        out.append(_format_stem3("vstem3", v_pairs))
    else:
        for p, w in v_pairs:
            out.append(_format_stem("vstem", p, w))

    return out


def _segment_anchor_index(ufo_contour, segment_idx: int, is_line: bool) -> int | None:
    """Find the UFO point index for the hint anchor of a path segment.

    UFO contour layout mirrors the AFDKO ``glyphData.drawPoints`` algorithm:
    index 0 is the wrap point (closing point of the subpath); subsequent
    points come from segments in order. A line segment contributes 1
    on-curve point; a curve segment contributes 2 off-curve + 1 on-curve.

    The hint anchor (per ``addUfoHints``) is the END point for a line
    segment and the FIRST off-curve control point for a curve segment.

    Returns the index of the anchor point in ``ufo_contour.points``, or
    None if the contour is too short to contain the requested segment.
    """
    pts = ufo_contour.points
    pi = 1  # skip wrap point at index 0
    seg = 0
    while pi < len(pts):
        pt = pts[pi]
        if pt.type == "line":
            if seg == segment_idx:
                return pi if is_line else None
            seg += 1
            pi += 1
        elif pt.type == "offcurve":
            if seg == segment_idx:
                return pi if not is_line else None
            seg += 1
            pi += 3  # 2 off-curves + 1 on-curve "curve"
        else:
            pi += 1
    return None


def _allocate_hint_ref(used_names: set[str], counter: list[int]) -> str:
    """Generate the next unused ``hintRefNNNN`` name."""
    while True:
        candidate = f"hintRef{counter[0]:04d}"
        counter[0] += 1
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate


def _build_hint_set_list(gd, ufo_glyph) -> list[dict[str, Any]]:
    """Walk a glyphData and the UFO outline in lockstep, attaching
    hintRef names and producing the hintSetList entries.

    Each hint event in the charstring (``startmasks`` plus any
    pathElement carrying a ``masks`` field) becomes one hintSetList
    entry; the entry's ``pointTag`` references the UFO point at which
    that hint set becomes active.
    """
    used_names: set[str] = {
        p.name for c in ufo_glyph for p in c.points if p.name
    }
    counter = [0]
    hint_set_list: list[dict[str, Any]] = []

    # Initial hint set is always emitted (matching AFDKO addUfoHints
    # at startSubpath): when gd.startmasks is None, all stems are
    # treated as active. This is required so makeotf finds an anchor
    # point even for glyphs with no hint substitution.
    if (gd.hstems or gd.vstems) and len(ufo_glyph) > 0 and len(ufo_glyph[0].points) > 0:
        wrap_pt = ufo_glyph[0].points[0]
        if not wrap_pt.name:
            wrap_pt.name = _allocate_hint_ref(used_names, counter)
        else:
            used_names.add(wrap_pt.name)
        hint_set_list.append({
            "pointTag": wrap_pt.name,
            "stems": _decode_active_stems(
                gd.startmasks, gd.hstems, gd.vstems, gd.cntr
            ),
        })

    for sp_idx, subpath in enumerate(gd.subpaths):
        if sp_idx >= len(ufo_glyph):
            break
        ufo_contour = ufo_glyph[sp_idx]
        # Last pathElement closes the subpath and is drawn as the wrap
        # point (already named above for subpath 0 via startmasks).
        for pe_idx, pe in enumerate(subpath[:-1]):
            if not pe.masks:
                continue
            anchor_idx = _segment_anchor_index(ufo_contour, pe_idx, pe.isLine())
            if anchor_idx is None:
                continue
            pt = ufo_contour.points[anchor_idx]
            if not pt.name:
                pt.name = _allocate_hint_ref(used_names, counter)
            else:
                used_names.add(pt.name)
            hint_set_list.append({
                "pointTag": pt.name,
                "stems": _decode_active_stems(
                    pe.masks, gd.hstems, gd.vstems, gd.cntr
                ),
            })

    return hint_set_list


# ── Build UFO hint dict ──────────────────────────────────────────────────────


def _build_hint_dict(
    glyph_name: str,
    charstring,
    ufo_glyph,
    outline_hash: str,
) -> dict[str, Any] | None:
    """Build com.adobe.type.autohint.v2 dict from a CFF charstring.

    Uses ``afdko.otfautohint.otfFont.convertT2ToGlyphData`` to parse the
    charstring (stems + hintmask events) and emits one hintSetList entry
    per hint-substitution point. For glyphs without hint substitution,
    a single entry is produced.
    """
    from afdko.otfautohint.otfFont import convertT2ToGlyphData

    try:
        gd = convertT2ToGlyphData(
            charstring,
            readStems=True,
            readFlex=False,
            roundCoords=True,
            name=glyph_name,
        )
    except Exception as e:
        logger.debug(f"convertT2ToGlyphData failed for '{glyph_name}': {e}")
        return None

    if not gd.hstems and not gd.vstems:
        return None

    hint_set_list = _build_hint_set_list(gd, ufo_glyph)
    if not hint_set_list:
        # Hints exist but we couldn't anchor them (degenerate outline).
        # Fall back to a single entry without pointTag.
        all_stems = [
            _format_stem("hstem", *s.UFOVals()) for s in gd.hstems
        ] + [
            _format_stem("vstem", *s.UFOVals()) for s in gd.vstems
        ]
        if not all_stems:
            return None
        hint_set_list = [{"stems": all_stems}]

    return {
        "formatVersion": "1",
        "id": outline_hash,
        "hintSetList": hint_set_list,
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

        ufo_glyph = ufo_font[glyph_name]

        # Compute outline hash for staleness detection
        try:
            outline_hash = compute_outline_hash(ufo_glyph)
        except Exception:
            outline_hash = ""

        # Build the hint dict (parses charstring via afdko.otfautohint)
        try:
            hint_dict = _build_hint_dict(
                glyph_name, charstring, ufo_glyph, outline_hash
            )
        except Exception as e:
            error_count += 1
            if error_count <= 5:
                logger.debug(f"Hint extraction failed for '{glyph_name}': {e}")
            continue

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

    # UFO has no postscriptStdHW/StdVW field, but ufo2ft derives StdHW
    # from postscriptStemSnapH[0] when compiling. Ensure CFF's StdHW value
    # is the first entry of StemSnapH so it survives the roundtrip.
    for cff_std, cff_snap, ufo_snap in [
        ("StdHW", "StemSnapH", "postscriptStemSnapH"),
        ("StdVW", "StemSnapV", "postscriptStemSnapV"),
    ]:
        std_val = getattr(private, cff_std, None)
        if std_val is None:
            continue
        snap = list(getattr(info, ufo_snap, None) or [])
        if not snap:
            snap = list(getattr(private, cff_snap, []) or [])
        if std_val in snap:
            snap.remove(std_val)
        snap.insert(0, std_val)
        setattr(info, ufo_snap, snap)

    return warnings
