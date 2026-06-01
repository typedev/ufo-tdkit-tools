# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for OS/2 fsSelection preservation in extraction.converter.

ufo-extractor (through 0.8.1) keeps only fsSelection bits 1-4 and drops bits 7
(USE_TYPO_METRICS), 8 (WWS) and 9 (OBLIQUE). _preserve_os2_selection restores
the spec-permitted bits straight from the source OS/2 table.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from ufo_tdkit_tools.extraction.converter import _preserve_os2_selection


def _fake_ufo():
    return SimpleNamespace(info=SimpleNamespace(openTypeOS2Selection=None))


class TestPreserveOS2Selection:
    def test_restores_use_typo_metrics_wws_oblique(self):
        # bits 0,5,6 (ITALIC/BOLD/REGULAR) + 7,8,9 set
        fs = (1 << 0) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9)
        tt = {"OS/2": SimpleNamespace(fsSelection=fs)}
        ufo = _fake_ufo()
        _preserve_os2_selection(tt, ufo)
        # 0,5,6 are derived from style -> excluded; 7,8,9 kept
        assert ufo.info.openTypeOS2Selection == [7, 8, 9]

    def test_keeps_low_bits(self):
        fs = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)
        tt = {"OS/2": SimpleNamespace(fsSelection=fs)}
        ufo = _fake_ufo()
        _preserve_os2_selection(tt, ufo)
        assert ufo.info.openTypeOS2Selection == [1, 2, 3, 4]

    def test_use_typo_metrics_alone(self):
        tt = {"OS/2": SimpleNamespace(fsSelection=1 << 7)}
        ufo = _fake_ufo()
        _preserve_os2_selection(tt, ufo)
        assert ufo.info.openTypeOS2Selection == [7]

    def test_empty_selection(self):
        tt = {"OS/2": SimpleNamespace(fsSelection=0)}
        ufo = _fake_ufo()
        _preserve_os2_selection(tt, ufo)
        assert ufo.info.openTypeOS2Selection == []

    def test_excludes_derived_bits_only(self):
        # only ITALIC + BOLD + REGULAR set -> nothing explicit
        fs = (1 << 0) | (1 << 5) | (1 << 6)
        tt = {"OS/2": SimpleNamespace(fsSelection=fs)}
        ufo = _fake_ufo()
        _preserve_os2_selection(tt, ufo)
        assert ufo.info.openTypeOS2Selection == []

    def test_no_os2_table_is_noop(self):
        ufo = _fake_ufo()
        ufo.info.openTypeOS2Selection = "untouched"
        _preserve_os2_selection({}, ufo)
        assert ufo.info.openTypeOS2Selection == "untouched"


# ── Integration: full binary -> UFO round trip ───────────────────────────────


@pytest.mark.skipif(
    shutil.which("tx") is None,
    reason="afdko tx not on PATH",
)
def test_use_typo_metrics_survives_extraction(tmp_path):
    """A binary OTF with USE_TYPO_METRICS extracts to a UFO that keeps it."""
    pytest.importorskip("extractor")
    pytest.importorskip("ufo2ft")
    defcon = pytest.importorskip("defcon")
    from ufo2ft import compileOTF

    from ufo_tdkit_tools.extraction.converter import convert_binary_to_ufo

    # Build a tiny source UFO with USE_TYPO_METRICS (bit 7) + WWS (bit 8).
    font = defcon.Font()
    info = font.info
    info.familyName = "Sel"
    info.styleName = "Regular"
    info.postscriptFontName = "Sel-Regular"
    info.unitsPerEm = 1000
    info.ascender = 750
    info.descender = -250
    info.openTypeOS2Selection = [7, 8]
    nd = font.newGlyph(".notdef")
    nd.width = 500
    g = font.newGlyph("A")
    g.width = 600
    g.unicode = ord("A")
    pen = g.getPen()
    pen.moveTo((100, 0))
    pen.lineTo((500, 0))
    pen.lineTo((500, 700))
    pen.lineTo((100, 700))
    pen.closePath()
    src_ufo = tmp_path / "src.ufo"
    font.save(str(src_ufo))

    src_otf = tmp_path / "src.otf"
    compileOTF(defcon.Font(str(src_ufo)), optimizeCFF=1).save(str(src_otf))

    result = convert_binary_to_ufo(src_otf)
    try:
        selection = result.font.info.openTypeOS2Selection or []
        assert 7 in selection, "USE_TYPO_METRICS (bit 7) must survive extraction"
        assert 8 in selection, "WWS (bit 8) must survive extraction"
    finally:
        result.temp_dir.cleanup()
