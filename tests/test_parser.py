# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for PS hints parser."""

from ufo_tdkit_tools.ps_hints.parser import (
    HintSource,
    PSHint,
    PSHintData,
    PSHintSet,
    parse_stem,
)


class TestParseStem:
    def test_hstem(self):
        h = parse_stem("hstem 50 20")
        assert h is not None
        assert h.type == "hstem"
        assert h.position == 50.0
        assert h.width == 20.0
        assert h.is_horizontal
        assert not h.is_vertical
        assert not h.is_triple
        assert not h.is_ghost

    def test_vstem(self):
        h = parse_stem("vstem 100 18")
        assert h is not None
        assert h.type == "vstem"
        assert h.position == 100.0
        assert h.width == 18.0
        assert h.is_vertical
        assert not h.is_horizontal

    def test_hstem3(self):
        h = parse_stem("hstem3 0 10 200 30 400 40")
        assert h is not None
        assert h.type == "hstem3"
        assert h.is_triple
        assert h.pairs == [(0, 10), (200, 30), (400, 40)]

    def test_vstem3(self):
        h = parse_stem("vstem3 10 20 100 20 190 20")
        assert h is not None
        assert h.type == "vstem3"
        assert h.is_triple
        assert len(h.pairs) == 3

    def test_ghost_top(self):
        h = parse_stem("hstem 700 -20")
        assert h is not None
        assert h.is_ghost
        assert h.is_top_ghost
        assert not h.is_bottom_ghost

    def test_ghost_bottom(self):
        h = parse_stem("hstem 0 -21")
        assert h is not None
        assert h.is_ghost
        assert h.is_bottom_ghost

    def test_end_property(self):
        h = parse_stem("vstem 100 50")
        assert h.end == 150.0

    def test_end_ghost(self):
        h = parse_stem("hstem 700 -20")
        assert h.end == 700.0  # Ghost hints return position

    def test_invalid_empty(self):
        assert parse_stem("") is None

    def test_invalid_short(self):
        assert parse_stem("hstem") is None

    def test_invalid_type(self):
        assert parse_stem("foobar 10 20") is None

    def test_invalid_values(self):
        assert parse_stem("hstem abc def") is None

    def test_hstem3_too_few(self):
        assert parse_stem("hstem3 10 20 30") is None

    def test_raw_preserved(self):
        h = parse_stem("hstem 50 20")
        assert h.raw == "hstem 50 20"


class TestPSHintData:
    def test_total_stems(self):
        data = PSHintData(
            source=HintSource.AUTOHINT_V2,
            hint_sets=[
                PSHintSet(stems=[
                    PSHint(type="hstem", position=0, width=50, raw="hstem 0 50"),
                    PSHint(type="vstem", position=100, width=18, raw="vstem 100 18"),
                ]),
                PSHintSet(stems=[
                    PSHint(type="hstem", position=0, width=50, raw="hstem 0 50"),  # dup
                    PSHint(type="vstem", position=200, width=20, raw="vstem 200 20"),
                ]),
            ],
        )
        assert data.total_stems == 3  # 3 unique

    def test_has_hint_substitution(self):
        data = PSHintData(
            source=HintSource.AUTOHINT_V2,
            hint_sets=[PSHintSet(), PSHintSet()],
        )
        assert data.has_hint_substitution

    def test_no_hint_substitution(self):
        data = PSHintData(
            source=HintSource.AUTOHINT_V2,
            hint_sets=[PSHintSet()],
        )
        assert not data.has_hint_substitution


class TestHintSource:
    def test_values(self):
        assert HintSource.PROCESSED_LAYER.value == "processedglyphs"
        assert HintSource.AUTOHINT_V2.value == "autohint_v2"
        assert HintSource.PUBLIC_PS.value == "public_ps"
