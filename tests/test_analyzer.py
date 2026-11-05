# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for ps_hints.analyzer."""

from __future__ import annotations

from ufo_tdkit_tools.ps_hints.analyzer import analyze_glyph
from ufo_tdkit_tools.ps_hints.parser import HintSource, PSHintData, PSHintSet, parse_stem


def _data(*sets):
    return PSHintData(
        source=HintSource.PROCESSED_LAYER,
        hint_sets=[
            PSHintSet(
                point_tag=f"hintRef{i:04d}" if i else None,
                stems=[parse_stem(r) for r in raws],
                index=i,
            )
            for i, raws in enumerate(sets)
        ],
    )


def _tags(data, width=700):
    issues = analyze_glyph(data, width, [100], [52], 1000) or []
    return [tag for tag, _ in issues], issues


class TestOverlapWithinSet:
    def test_ghosts_inside_stems_are_reported(self):
        # cyrZhecyrbreve, SchoolBookNew-Regular: both ghosts sit inside a real stem
        data = _data(
            [
                "hstem -14 66",
                "hstem 21 -21",
                "hstem 338 53",
                "hstem 663 52",
                "hstem 700 -20",
                "vstem 513 106",
            ]
        )
        tags, issues = _tags(data)
        details = [d for t, d in issues if t == "overlap-set-h"]
        assert tags.count("overlap-set-h") == 2
        assert "set 0: hstem -14 66 / hstem 21 -21" in details
        assert "set 0: hstem 663 52 / hstem 700 -20" in details
        assert "overlap-h" not in tags  # the pooled check still ignores ghosts

    def test_ghost_on_stem_edge_is_not_overlap(self):
        data = _data(["hstem 0 50", "hstem 50 -20", "hstem 71 -21"])
        tags, _ = _tags(data)
        assert "overlap-set-h" not in tags

    def test_two_real_stems_in_one_set(self):
        tags, issues = _tags(_data(["vstem 100 100", "vstem 150 100"], ["hstem 0 50"]))
        assert ("overlap-set-v", "set 0: vstem 100 100 / vstem 150 100") in issues
        assert "overlap-v" in tags

    def test_overlap_across_sets_is_not_set_overlap(self):
        # otfautohint's B: hint replacement swaps one vstem for the other
        data = _data(["vstem 515 100"], ["vstem 465 100"])
        tags, issues = _tags(data)
        assert "overlap-set-v" not in tags
        assert "overlap-v" in tags  # pooled check: the optimizer keeps one

    def test_set_label_carries_point_tag(self):
        data = _data(["hstem 0 50"], ["hstem 0 50", "hstem 40 -20"])
        _, issues = _tags(data)
        assert ("overlap-set-h", "set 1 (hintRef0001): hstem 0 50 / hstem 40 -20") in issues
