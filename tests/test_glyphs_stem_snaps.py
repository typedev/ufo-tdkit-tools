# Copyright 2026 TypeDev
# Licensed under the Apache License, Version 2.0

"""
Tests for cleaning the stem snap lists of an imported Glyphs source.

Headless. Glyphs 3 stores one value per font-level stem definition on every
master, 0 where the master has none; UFOs merged into one Glyphs file come out
with lists mostly made of zeros, which glyphsLib copies into the UFO as is.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")
ufoLib2 = pytest.importorskip("ufoLib2")

from ufo_tdkit_tools.glyphs import convert_glyphs_to_ufos  # noqa: E402
from ufo_tdkit_tools.glyphs.stem_snaps import clean_stem_snaps  # noqa: E402


def _ufo(h=None, v=None) -> "ufoLib2.Font":
    font = ufoLib2.Font()
    font.info.postscriptStemSnapH = h
    font.info.postscriptStemSnapV = v
    return font


def test_zeros_and_repeats_are_dropped_order_kept():
    """Index 0 is what ufo2ft writes as StdHW / StdVW, so the order stays."""
    font = _ufo(h=[0, 0, 67, 41, 0, 67], v=[0, 74, 54])

    warnings = clean_stem_snaps(font, "Book.ufo")

    assert font.info.postscriptStemSnapH == [67, 41]
    assert font.info.postscriptStemSnapV == [74, 54]
    assert len(warnings) == 2
    assert all(w.category == "preflight" for w in warnings)
    assert "Book.ufo" in warnings[0].message


def test_all_zero_list_is_unset_and_reported():
    font = _ufo(h=[0, 0, 0], v=[0])

    warnings = clean_stem_snaps(font, "Bold.ufo")

    assert font.info.postscriptStemSnapH is None
    assert font.info.postscriptStemSnapV is None
    assert len(warnings) == 2
    assert all(w.category == "stems" for w in warnings)


def test_clean_lists_are_left_alone():
    font = _ufo(h=[41, 67], v=None)

    assert clean_stem_snaps(font, "Book.ufo") == []
    assert font.info.postscriptStemSnapH == [41, 67]
    assert font.info.postscriptStemSnapV is None


def test_import_writes_no_zero_stems(tmp_path: Path):
    """Two masters with stems of their own: each holds 0 in the other's slots."""
    gs_font = glyphsLib.GSFont()
    gs_font.familyName = "Stems"
    for name, horizontal in (("h-light", True), ("h-bold", True), ("v-light", False)):
        stem = glyphsLib.classes.GSMetric()
        stem.name = name
        stem.horizontal = horizontal
        gs_font.stems.append(stem)
    for name, stems in (("Light", [30, 0, 40]), ("Bold", [0, 0, 0])):
        master = glyphsLib.GSFontMaster()
        master.name = name
        master.stems = stems
        gs_font.masters.append(master)
    source = tmp_path / "Stems.glyphs"
    with open(source, "w", encoding="utf-8") as fp:
        glyphsLib.dump(gs_font, fp)

    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    infos = {p.name: ufoLib2.Font.open(p).info for p in result.ufo_paths}
    light = next(info for name, info in infos.items() if "Light" in name)
    bold = next(info for name, info in infos.items() if "Bold" in name)
    assert light.postscriptStemSnapH == [30]
    assert light.postscriptStemSnapV == [40]
    assert bold.postscriptStemSnapH is None
    assert bold.postscriptStemSnapV is None
    assert any(w.category == "stems" for w in result.warnings)
