# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Synthesize a legacy format-0 ``kern`` table from a font's GPOS kerning.

Microsoft Word (and other older/Office rendering paths) reads horizontal
kerning from the old-style ``kern`` table rather than from GPOS. Modern build
tools (ufo2ft/fontmake) emit kerning only as GPOS pair positioning, so TTFs
produced by them lose kerning in Word even though the data is present. Legacy
ParaType TTFs shipped the ``kern`` table as a compatibility fallback; this
module regenerates it from the font's own GPOS ``kern`` feature.

Design notes
------------
* Only horizontal advance adjustments (``Value1.XAdvance``) from **pair
  positioning** lookups (GPOS LookupType 2, plain or extension-wrapped) are
  collected — a format-0 ``kern`` subtable can express nothing else, and
  typical LTR pair kerning uses ``ValueFormat1 = 0x0004`` (XAdvance only)
  anyway. A ``kern`` feature may also carry contextual lookups (kerning
  exceptions, LookupType 8); those have no format-0 equivalent, so they are
  skipped and logged rather than silently mistaken for pair positioning.
* Overlap resolution is **first-wins**: the first lookup/subtable (in feature
  order) that yields a given pair claims it; later ones neither overwrite nor
  sum. This mirrors OpenType's "the first subtable covering the first glyph
  handles the pair" semantics and avoids double-counting a format-1 exception
  that also falls inside a format-2 class cell.
* Right-hand class 0 in format-2 subtables ("all other glyphs", almost always
  value 0) is skipped — enumerating it would explode the table for no gain.
* :func:`add_legacy_kern` guarantees that *only* the ``kern`` table changes:
  GPOS is flattened on a throwaway ``TTFont`` so the saved instance never
  decompiles GPOS/glyf (with ``lazy=True`` those tables are copied back
  verbatim), and ``recalcTimestamp=False`` preserves ``head.modified``. The
  only unavoidable side effect is ``head.checkSumAdjustment`` (a whole-file
  checksum that must reflect the added table).
* The format-0 subtable ``length`` field is uint16 and overflows for more than
  ~10920 pairs (~64 KB). This is benign and widespread (legacy ParaType TTFs,
  plus fonts like OpenSans/Calibri, have the same overflow): consumers use
  ``nPairs`` and ignore ``length`` for a single format-0 subtable. fontTools
  handles it on both read and write. When there are more than 65535 pairs the
  table is split across several format-0 subtables (each is a valid MS subtable
  and Word accumulates them).

This module depends only on ``fontTools``.

Public API
----------
* :func:`flatten_gpos_kern` — GPOS ``kern`` feature → ``{(left, right): xAdvance}``
* :func:`build_kern_table` — pair dict → a ``kern`` table object (format 0, v0)
* :func:`add_legacy_kern` — add the synthesized ``kern`` to a TTF file in place
"""

from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)

# Signed int16 bounds for format-0 kern values.
_INT16_MIN = -32768
_INT16_MAX = 32767
# nPairs is a uint16; a single format-0 subtable can hold at most this many.
_MAX_PAIRS_PER_SUBTABLE = 0xFFFF
# MS format-0 subtable coverage: bit 0 set == horizontal.
_COVERAGE_HORIZONTAL = 0x0001


def flatten_gpos_kern(font) -> dict[tuple[str, str], int]:
    """Flatten a font's GPOS ``kern`` feature to ``{(left, right): xAdvance}``.

    ``font`` is an open ``fontTools.ttLib.TTFont``. Returns an empty dict when
    the font has no GPOS or no ``kern`` feature. See the module docstring for
    the XAdvance-only / first-wins / right-class-0 rules.
    """
    if "GPOS" not in font:
        return {}
    gpos = font["GPOS"].table
    if not gpos or not gpos.LookupList or not gpos.FeatureList:
        return {}

    # Collect kern-feature lookup indices in feature order, de-duplicated
    # (lookups are shared across scripts/languages).
    kern_lookups: list[int] = []
    seen: set[int] = set()
    for fr in gpos.FeatureList.FeatureRecord:
        if fr.FeatureTag != "kern":
            continue
        for li in fr.Feature.LookupListIndex:
            if li not in seen:
                seen.add(li)
                kern_lookups.append(li)

    all_glyphs = set(font.getGlyphOrder())
    pairs: dict[tuple[str, str], int] = {}

    def record(left: str, right: str, xadv: int) -> None:
        if not xadv:
            return
        key = (left, right)
        if key not in pairs:  # first-wins
            pairs[key] = xadv

    skipped: set[int] = set()

    def pairpos_subtables(lk):
        """Yield the lookup's PairPos subtables, unwrapping Extension Positioning.

        Anything else in the kern feature (contextual kerning exceptions,
        cursive attachment, ...) has no format-0 equivalent: it cannot be
        translated, and its subtables do not even share PairPos's shape.
        """
        for st in lk.SubTable:
            lookup_type = lk.LookupType
            if lookup_type == 9:  # Extension Positioning
                lookup_type = getattr(st, "ExtensionLookupType", None)
                st = getattr(st, "ExtSubTable", None)
            if lookup_type != 2 or st is None:  # 2 == PairPos
                logger.debug(
                    "kern feature: skipping non-PairPos lookup (type %s)", lookup_type
                )
                skipped.add(li)
                continue
            yield st

    for li in kern_lookups:
        lk = gpos.LookupList.Lookup[li]
        for st in pairpos_subtables(lk):
            fmt = getattr(st, "Format", None)
            if fmt == 1:
                for left, pairset in zip(st.Coverage.glyphs, st.PairSet):
                    for pvr in pairset.PairValueRecord:
                        v = getattr(pvr, "Value1", None)
                        record(left, pvr.SecondGlyph, getattr(v, "XAdvance", 0) if v else 0)
            elif fmt == 2:
                cd1 = dict(st.ClassDef1.classDefs) if st.ClassDef1 else {}
                cd2 = dict(st.ClassDef2.classDefs) if st.ClassDef2 else {}
                lefts_by_class: dict[int, list[str]] = {}
                for g in st.Coverage.glyphs:  # class 0 included (own row)
                    lefts_by_class.setdefault(cd1.get(g, 0), []).append(g)
                rights_by_class: dict[int, list[str]] = {}
                for g in all_glyphs:
                    cl = cd2.get(g, 0)
                    if cl:  # skip right-class 0 ("all other glyphs")
                        rights_by_class.setdefault(cl, []).append(g)
                for ci, c1rec in enumerate(st.Class1Record):
                    lefts = lefts_by_class.get(ci)
                    if not lefts:
                        continue
                    for cj, c2rec in enumerate(c1rec.Class2Record):
                        if cj == 0:
                            continue
                        v = getattr(c2rec, "Value1", None)
                        xadv = getattr(v, "XAdvance", 0) if v else 0
                        if not xadv:
                            continue
                        rights = rights_by_class.get(cj)
                        if not rights:
                            continue
                        for lg in lefts:
                            for rg in rights:
                                record(lg, rg, xadv)

    if skipped:
        logger.info(
            "kern feature: %d of %d lookup(s) are not pair positioning and have no "
            "format-0 equivalent; their kerning is not in the legacy table",
            len(skipped), len(kern_lookups),
        )
    return pairs


def _clamp_int16(pairs: dict[tuple[str, str], int]) -> dict[tuple[str, str], int]:
    """Clamp values to signed int16, warning on any that overflow (rare)."""
    clean: dict[tuple[str, str], int] = {}
    for key, val in pairs.items():
        if val < _INT16_MIN or val > _INT16_MAX:
            logger.warning("kern pair %s value %d out of int16 range; clamped", key, val)
            val = max(_INT16_MIN, min(_INT16_MAX, val))
        clean[key] = val
    return clean


def _new_format0_subtable(pairs: dict[tuple[str, str], int]):
    """Build one horizontal format-0 kern subtable from a pair→value dict."""
    from fontTools.ttLib.tables._k_e_r_n import KernTable_format_0

    st = KernTable_format_0()  # apple=False -> MS/OpenType subtable
    st.version = 0
    st.format = 0
    st.coverage = _COVERAGE_HORIZONTAL
    st.tupleIndex = None
    st.kernTable = dict(pairs)
    return st


def build_kern_table(pairs: dict[tuple[str, str], int]):
    """Build a version-0 ``kern`` table object from a pair→value dict.

    Values are clamped to signed int16. Pairs are split across multiple
    format-0 subtables when they exceed the uint16 ``nPairs`` limit. Returns
    ``None`` when ``pairs`` is empty.
    """
    from fontTools.ttLib import newTable

    clean = _clamp_int16(pairs)
    if not clean:
        return None

    kern = newTable("kern")
    kern.version = 0
    kern.kernTables = []

    items = list(clean.items())
    for start in range(0, len(items), _MAX_PAIRS_PER_SUBTABLE):
        chunk = dict(items[start : start + _MAX_PAIRS_PER_SUBTABLE])
        kern.kernTables.append(_new_format0_subtable(chunk))
    if len(items) > _MAX_PAIRS_PER_SUBTABLE:
        logger.warning(
            "%d kern pairs split across %d format-0 subtables",
            len(items), len(kern.kernTables),
        )
    return kern


def add_legacy_kern(ttf_path: str | Path) -> int:
    """Add a legacy format-0 ``kern`` table (from GPOS) to a TTF, in place.

    Returns the number of pairs written (0 if the font has no GPOS kerning, in
    which case the file is left untouched). Only the ``kern`` table is added;
    every other table is preserved byte-for-byte except the mandatory
    ``head.checkSumAdjustment``. See the module docstring for the mechanism.
    """
    from fontTools.ttLib import TTFont

    ttf_path = Path(ttf_path)

    # Flatten GPOS on a throwaway instance so the instance we save never
    # decompiles GPOS/glyf; lazy tables that are never touched are written back
    # verbatim, guaranteeing only 'kern' changes.
    reader = TTFont(str(ttf_path), lazy=True)
    try:
        pairs = flatten_gpos_kern(reader)
    finally:
        reader.close()

    kern = build_kern_table(pairs)
    if kern is None:
        return 0

    # recalcTimestamp=False keeps head.modified; only head.checkSumAdjustment
    # (a whole-file checksum) changes.
    font = TTFont(str(ttf_path), lazy=True, recalcTimestamp=False)
    # lazy=True forbids in-place overwrite (the reader still maps the original
    # bytes for verbatim tables); save beside it, then replace.
    tmp = ttf_path.with_suffix(ttf_path.suffix + ".kern.tmp")
    try:
        font["kern"] = kern
        font.save(str(tmp))
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave a half-written font behind
        raise
    finally:
        font.close()
    tmp.replace(ttf_path)

    return sum(len(st.kernTable) for st in kern.kernTables)
