# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for ps_hints.optimizer."""

from __future__ import annotations

import pytest

from ufo_tdkit_tools.ps_hints.optimizer import optimize_hints
from ufo_tdkit_tools.ps_hints.parser import HintSource, PSHintData, PSHintSet, parse_stem

fp = pytest.importorskip("fontParts.world")

XHEIGHT = 547
STEM = [(63, 547), (63, 0), (137, 0), (137, 547)]
TITTLE = [(63, 740), (63, 614), (137, 614), (137, 740)]


def _glyph(name, unicode_, contours, x_height=XHEIGHT):
    font = fp.NewFont()
    font.info.xHeight = x_height
    font.info.capHeight = 700
    font.info.ascender = 740
    glyph = font.newGlyph(name)
    if unicode_ is not None:
        glyph.unicodes = [unicode_]
    pen = glyph.getPen()
    for pts in contours:
        pen.moveTo(pts[0])
        for pt in pts[1:]:
            pen.lineTo(pt)
        pen.closePath()
    glyph.width = 200
    return glyph


def _hint_data(raws):
    stems = [parse_stem(r) for r in raws]
    return PSHintData(
        source=HintSource.PROCESSED_LAYER,
        hint_sets=[PSHintSet(point_tag=None, point_coords=None, stems=stems, index=0)],
    )


def _optimized(glyph, raws, snap_v=(74,), snap_h=(41, 67)):
    result = optimize_hints(
        _hint_data(raws),
        glyph_width=glyph.width,
        stem_snap_v=list(snap_v),
        stem_snap_h=list(snap_h),
        glyph=glyph,
    )
    return {s.raw for s in result.hint_sets[0].stems}


I_HINTS = ["hstem 21 -21", "hstem 547 -20", "hstem 614 126", "vstem 63 74"]


class TestTittleFilter:
    @pytest.mark.parametrize(
        "name,unicode_",
        [("i", 0x69), ("j", 0x6A), ("uni0456", 0x456), ("uni0458", 0x458), ("i.alt", None)],
    )
    def test_tittle_hstem_dropped(self, name, unicode_):
        glyph = _glyph(name, unicode_, [STEM, TITTLE])
        stems = _optimized(glyph, I_HINTS)
        assert "hstem 614 126" not in stems
        assert {"hstem 21 -21", "hstem 547 -20", "vstem 63 74"} <= stems

    def test_tittle_dropped_under_below_mark(self):
        # į keeps its tittle: NFD gives i + ogonek, which alone cuts only below.
        glyph = _glyph("iogonek", 0x12F, [STEM, TITTLE])
        assert "hstem 614 126" not in _optimized(glyph, I_HINTS)

    def test_kept_without_detached_contour_above(self):
        # A small-cap i is one contour reaching above xHeight: no tittle to cut.
        tall_stem = [(63, 620), (63, 0), (137, 0), (137, 620)]
        glyph = _glyph("i.sc", None, [tall_stem])
        stems = _optimized(glyph, ["hstem 590 30", "vstem 63 74"])
        assert "hstem 590 30" in stems

    def test_dotless_i_untouched(self):
        glyph = _glyph("dotlessi", 0x131, [STEM])
        assert _optimized(glyph, ["hstem 21 -21", "hstem 547 -20", "vstem 63 74"]) == {
            "hstem 21 -21",
            "hstem 547 -20",
            "vstem 63 74",
        }

    def test_other_dotted_glyphs_untouched(self):
        glyph = _glyph("l", 0x6C, [[(63, 740), (63, 0), (137, 0), (137, 740)]])
        assert "hstem 600 40" in _optimized(glyph, ["hstem 600 40", "vstem 63 74"])


class TestZeroStemSnaps:
    def test_zero_padding_ignored(self):
        glyph = _glyph("l", 0x6C, [[(63, 740), (63, 0), (137, 0), (137, 740)]])
        stems = _optimized(glyph, ["vstem 63 74"], snap_v=(74, 0, 0), snap_h=(0, 0))
        assert "vstem 63 74" in stems

    def test_all_zero_snaps_fall_back_to_upm(self):
        glyph = _glyph("l", 0x6C, [[(63, 740), (63, 0), (137, 0), (137, 740)]])
        stems = _optimized(glyph, ["vstem 63 74"], snap_v=(0, 0, 0), snap_h=(0, 0))
        assert "vstem 63 74" in stems
