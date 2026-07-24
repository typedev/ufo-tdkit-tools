# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for compilation.legacy_kern (GPOS kerning -> legacy 'kern' table).

Needs only fontTools: the fixtures are built with ``FontBuilder`` and feaLib.
"""

from __future__ import annotations

import pytest
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from ufo_tdkit_tools.compilation.legacy_kern import (
    add_legacy_kern,
    build_kern_table,
    flatten_gpos_kern,
)

GLYPHS = [".notdef", "A", "V", "T", "o"]


def _build_ttf(path, fea: str | None = None):
    """A minimal TTF with five box glyphs and optional GPOS features."""
    from fontTools.fontBuilder import FontBuilder

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(GLYPHS)
    fb.setupCharacterMap({ord(name): name for name in GLYPHS if len(name) == 1})

    glyphs = {}
    for name in GLYPHS:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((450, 0))
        pen.lineTo((450, 700))
        pen.lineTo((50, 700))
        pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({name: (500, 50) for name in GLYPHS})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "KernProbe", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    if fea:
        fb.addOpenTypeFeatures(fea)
    fb.save(str(path))
    return path


def _raw_tables(path):
    """Raw bytes of every table in the file, keyed by tag."""
    font = TTFont(str(path), lazy=True)
    try:
        return {tag: font.reader[tag] for tag in font.reader.keys()}
    finally:
        font.close()


def _kern_pairs(path):
    font = TTFont(str(path))
    try:
        if "kern" not in font:
            return None
        pairs = {}
        for subtable in font["kern"].kernTables:
            pairs.update(subtable.kernTable)
        return pairs
    finally:
        font.close()


SIMPLE_FEA = """
@LEFT = [A T];
@RIGHT = [o];
feature kern {
    pos A V -80;
    pos @LEFT @RIGHT -40;
} kern;
"""


class TestFlattenGposKern:
    def test_singles_and_classes(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf", SIMPLE_FEA)
        font = TTFont(str(ttf))
        pairs = flatten_gpos_kern(font)
        font.close()
        assert pairs == {("A", "V"): -80, ("A", "o"): -40, ("T", "o"): -40}

    def test_no_gpos_returns_empty(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf")
        font = TTFont(str(ttf))
        pairs = flatten_gpos_kern(font)
        font.close()
        assert pairs == {}

    def test_non_kern_feature_ignored(self, tmp_path):
        fea = """
        feature kern { pos A V -80; } kern;
        feature dist { pos T o -25; } dist;
        """
        ttf = _build_ttf(tmp_path / "in.ttf", fea)
        font = TTFont(str(ttf))
        pairs = flatten_gpos_kern(font)
        font.close()
        assert pairs == {("A", "V"): -80}

    def test_contextual_kern_lookup_is_skipped_not_fatal(self, tmp_path):
        """A `kern` feature may hold contextual lookups (kerning exceptions).

        Those cannot be expressed in a format-0 kern subtable; they must be
        skipped, and the plain pairs alongside them must still come through.
        """
        fea = """
        feature kern {
            pos A V -80;
            pos T o' -30 A;
        } kern;
        """
        ttf = _build_ttf(tmp_path / "in.ttf", fea)
        font = TTFont(str(ttf))
        pairs = flatten_gpos_kern(font)
        font.close()
        assert pairs == {("A", "V"): -80}

    def test_zero_values_are_dropped(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf", "feature kern { pos A V 0; } kern;")
        font = TTFont(str(ttf))
        pairs = flatten_gpos_kern(font)
        font.close()
        assert pairs == {}


class TestBuildKernTable:
    def test_empty_returns_none(self):
        assert build_kern_table({}) is None

    def test_single_subtable(self):
        kern = build_kern_table({("A", "V"): -80})
        assert kern.version == 0
        assert len(kern.kernTables) == 1
        subtable = kern.kernTables[0]
        assert subtable.format == 0
        assert subtable.coverage == 0x0001  # horizontal
        assert subtable.apple is False
        assert subtable.kernTable == {("A", "V"): -80}

    def test_values_are_clamped_to_int16(self, caplog):
        with caplog.at_level("WARNING"):
            kern = build_kern_table({("A", "V"): 40000, ("A", "T"): -40000})
        assert kern.kernTables[0].kernTable == {("A", "V"): 32767, ("A", "T"): -32768}
        assert "out of int16 range" in caplog.text

    def test_splits_when_over_uint16_pairs(self):
        pairs = {(f"g{i}", "A"): -10 for i in range(70000)}
        kern = build_kern_table(pairs)
        assert len(kern.kernTables) == 2
        assert sum(len(st.kernTable) for st in kern.kernTables) == 70000


class TestAddLegacyKern:
    def test_adds_kern_and_reports_pair_count(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf", SIMPLE_FEA)
        assert add_legacy_kern(ttf) == 3
        assert _kern_pairs(ttf) == {("A", "V"): -80, ("A", "o"): -40, ("T", "o"): -40}

    def test_only_kern_and_checksum_change(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf", SIMPLE_FEA)
        before = _raw_tables(ttf)
        font = TTFont(str(ttf))
        modified_before = font["head"].modified
        font.close()

        add_legacy_kern(ttf)

        after = _raw_tables(ttf)
        assert set(after) - set(before) == {"kern"}
        for tag, data in before.items():
            if tag == "head":
                continue  # checkSumAdjustment covers the whole file
            assert after[tag] == data, f"table {tag} changed"

        font = TTFont(str(ttf))
        assert font["head"].modified == modified_before
        font.close()

    def test_font_without_gpos_is_left_untouched(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf")
        before = ttf.read_bytes()
        assert add_legacy_kern(ttf) == 0
        assert ttf.read_bytes() == before

    def test_no_temp_file_left_behind(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf", SIMPLE_FEA)
        add_legacy_kern(ttf)
        assert [p.name for p in tmp_path.iterdir()] == ["in.ttf"]

    def test_accepts_str_path(self, tmp_path):
        ttf = _build_ttf(tmp_path / "in.ttf", SIMPLE_FEA)
        assert add_legacy_kern(str(ttf)) == 3


@pytest.mark.parametrize("fea", [SIMPLE_FEA, None])
def test_roundtrip_is_idempotent(tmp_path, fea):
    """Running twice yields the same kern table (the second run replaces it)."""
    ttf = _build_ttf(tmp_path / "in.ttf", fea)
    first = add_legacy_kern(ttf)
    pairs_first = _kern_pairs(ttf)
    second = add_legacy_kern(ttf)
    assert first == second
    assert _kern_pairs(ttf) == pairs_first


class TestPackageExports:
    def test_exposed_from_compilation_subpackage(self):
        import ufo_tdkit_tools.compilation as compilation

        assert compilation.add_legacy_kern is add_legacy_kern
        assert compilation.build_kern_table is build_kern_table
        assert compilation.flatten_gpos_kern is flatten_gpos_kern

    def test_exposed_from_package_root(self):
        import ufo_tdkit_tools

        assert ufo_tdkit_tools.add_legacy_kern is add_legacy_kern
