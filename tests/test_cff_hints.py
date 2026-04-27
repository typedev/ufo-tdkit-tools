# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for CFF hint extraction helpers in extraction.cff_hints."""

from __future__ import annotations

from types import SimpleNamespace

from ufo_tdkit_tools.extraction.cff_hints import (
    _decode_active_stems,
    _segment_anchor_index,
)


class _FakeStem:
    """Minimal stem stub with the same UFOVals signature as glyphData.stem."""

    def __init__(self, pos, width):
        self._pos = pos
        self._width = width

    def UFOVals(self):
        return self._pos, self._width


class _FakePoint:
    def __init__(self, x, y, type_, name=None):
        self.x = x
        self.y = y
        self.type = type_
        self.name = name


class _FakeContour:
    def __init__(self, points):
        self.points = points


class TestDecodeActiveStems:
    def test_no_masks_returns_all_stems(self):
        hstems = [_FakeStem(0, 78), _FakeStem(319, 78)]
        vstems = [_FakeStem(85, 86)]
        out = _decode_active_stems(None, hstems, vstems)
        assert out == ["hstem 0 78", "hstem 319 78", "vstem 85 86"]

    def test_filters_by_h_mask_then_v_mask(self):
        hstems = [_FakeStem(0, 78), _FakeStem(319, 78), _FakeStem(613, 77)]
        vstems = [_FakeStem(85, 86), _FakeStem(448, 86), _FakeStem(470, 86)]
        # B's startmask: hstems all on, vstems on/off/on
        masks = [[True, True, True], [True, False, True]]
        out = _decode_active_stems(masks, hstems, vstems)
        assert out == [
            "hstem 0 78", "hstem 319 78", "hstem 613 77",
            "vstem 85 86", "vstem 470 86",
        ]

    def test_h_mask_only(self):
        hstems = [_FakeStem(0, 78), _FakeStem(100, 50)]
        masks = [[False, True], None]
        out = _decode_active_stems(masks, hstems, [])
        assert out == ["hstem 100 50"]

    def test_empty_stem_lists(self):
        assert _decode_active_stems(None, [], []) == []
        assert _decode_active_stems([[], []], [], []) == []


class TestSegmentAnchorIndex:
    def test_line_segment_endpoint(self):
        # Wrap point + 3 line segments
        pts = [
            _FakePoint(0, 0, "line"),  # wrap
            _FakePoint(10, 0, "line"),  # segment 0 endpoint
            _FakePoint(20, 0, "line"),  # segment 1 endpoint
            _FakePoint(30, 0, "line"),  # segment 2 endpoint
        ]
        c = _FakeContour(pts)
        assert _segment_anchor_index(c, 0, is_line=True) == 1
        assert _segment_anchor_index(c, 1, is_line=True) == 2
        assert _segment_anchor_index(c, 2, is_line=True) == 3

    def test_curve_segment_first_offcurve(self):
        # Wrap + 1 curve segment (2 off-curves + 1 on-curve "curve")
        pts = [
            _FakePoint(0, 0, "line"),  # wrap
            _FakePoint(5, 5, "offcurve"),  # 1st off-curve <-- anchor for is_line=False
            _FakePoint(8, 5, "offcurve"),  # 2nd off-curve
            _FakePoint(10, 0, "curve"),  # on-curve curve endpoint
        ]
        c = _FakeContour(pts)
        assert _segment_anchor_index(c, 0, is_line=False) == 1
        # is_line mismatch returns None
        assert _segment_anchor_index(c, 0, is_line=True) is None

    def test_mixed_line_and_curve(self):
        pts = [
            _FakePoint(0, 0, "line"),  # wrap
            _FakePoint(10, 0, "line"),  # segment 0: line endpoint
            _FakePoint(15, 5, "offcurve"),  # segment 1: 1st off-curve <-- anchor
            _FakePoint(18, 5, "offcurve"),
            _FakePoint(20, 0, "curve"),
            _FakePoint(30, 0, "line"),  # segment 2: line endpoint
        ]
        c = _FakeContour(pts)
        assert _segment_anchor_index(c, 0, is_line=True) == 1
        assert _segment_anchor_index(c, 1, is_line=False) == 2
        assert _segment_anchor_index(c, 2, is_line=True) == 5

    def test_out_of_range_returns_none(self):
        pts = [_FakePoint(0, 0, "line"), _FakePoint(10, 0, "line")]
        c = _FakeContour(pts)
        assert _segment_anchor_index(c, 5, is_line=True) is None


class TestBuildHintSetListIntegration:
    """Synthetic glyphData → hintSetList wiring."""

    def test_no_hints_returns_empty_list(self):
        from ufo_tdkit_tools.extraction.cff_hints import _build_hint_set_list

        gd = SimpleNamespace(
            hstems=[],
            vstems=[],
            startmasks=None,
            subpaths=[],
        )
        ufo_glyph = []
        assert _build_hint_set_list(gd, ufo_glyph) == []

    def test_simple_glyph_no_substitution_emits_one_set(self):
        from ufo_tdkit_tools.extraction.cff_hints import _build_hint_set_list

        gd = SimpleNamespace(
            hstems=[_FakeStem(0, 78)],
            vstems=[_FakeStem(10, 50)],
            startmasks=None,
            subpaths=[[]],  # one empty subpath, just to satisfy iteration
        )
        contour = _FakeContour([_FakePoint(0, 0, "line")])
        ufo_glyph = [contour]
        result = _build_hint_set_list(gd, ufo_glyph)
        assert len(result) == 1
        assert result[0]["pointTag"] == "hintRef0000"
        assert result[0]["stems"] == ["hstem 0 78", "vstem 10 50"]
        # The wrap point got named.
        assert ufo_glyph[0].points[0].name == "hintRef0000"
