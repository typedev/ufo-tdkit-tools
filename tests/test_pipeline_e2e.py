# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""End-to-end smoke tests for pipeline.process_font.

Creates a minimal UFO programmatically, runs it through the full pipeline,
and verifies output OTF + UFO are produced. Roundtrips the OTF back through
the pipeline.

Skipped when AFDKO (``afdko`` package and ``tx``/``makeotf`` binaries) is
not available.
"""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("afdko")
pytest.importorskip("defcon")
pytest.importorskip("ufo2ft")

if shutil.which("tx") is None or shutil.which("makeotf") is None:
    pytest.skip("AFDKO tx/makeotf not on PATH", allow_module_level=True)

from fontTools.ttLib import TTFont  # noqa: E402

from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2  # noqa: E402
from ufo_tdkit_tools.pipeline import process_font  # noqa: E402


# ── UFO factory ───────────────────────────────────────────────────────────────


def _build_minimal_ufo(ufo_path):
    """Build a tiny UFO with one rectangle glyph carrying a single v-stem hint."""
    import defcon

    font = defcon.Font()
    info = font.info
    info.familyName = "Smoke"
    info.styleName = "Regular"
    info.postscriptFontName = "Smoke-Regular"
    info.versionMajor = 1
    info.versionMinor = 0
    info.unitsPerEm = 1000
    info.ascender = 750
    info.descender = -250
    info.capHeight = 700
    info.xHeight = 500
    info.copyright = "Test"
    info.postscriptUnderlinePosition = -100
    info.postscriptUnderlineThickness = 50
    info.postscriptBlueValues = [-12, 0, 500, 512, 700, 712]
    info.postscriptOtherBlues = [-212, -200]
    info.postscriptStemSnapH = [50]
    info.postscriptStemSnapV = [80]
    info.postscriptForceBold = False

    # .notdef as an empty glyph
    notdef = font.newGlyph(".notdef")
    notdef.width = 500

    # 'A' as a 100x700 rectangle with one named on-curve point
    glyph = font.newGlyph("A")
    glyph.width = 600
    glyph.unicode = ord("A")
    pen = glyph.getPen()
    pen.moveTo((100, 0))
    pen.lineTo((500, 0))
    pen.lineTo((500, 700))
    pen.lineTo((100, 700))
    pen.closePath()

    # name first on-curve point so v2 pointTag has a target
    glyph[0][0].name = "hintRef0000"

    # Single v-stem covering the rectangle width (100..500 -> width 400)
    glyph.lib[ADOBE_HINT_KEY_V2] = {
        "hintSetList": [
            {
                "pointTag": "hintRef0000",
                "stems": ["vstem 100 400", "hstem 0 700"],
            }
        ],
    }

    font.save(str(ufo_path))


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestPipelineEndToEnd:
    def test_ufo_with_v2_hints_to_otf(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        ufo_out = tmp_path / "out.ufo"

        _build_minimal_ufo(ufo_in)
        result = process_font(ufo_in, otf_out, ufo_out, hint_source="v2", optimize=False)

        assert result.success, f"pipeline failed: {result.error}"
        assert result.hint_source_used == "autohint_v2"
        assert result.glyphs_with_hints >= 1
        assert otf_out.exists()
        assert ufo_out.exists()

        # OTF is a valid CFF font with the 'A' glyph
        tt = TTFont(str(otf_out))
        assert "CFF " in tt
        assert "A" in tt.getGlyphOrder()
        tt.close()

    def test_auto_source_picks_v2(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        _build_minimal_ufo(ufo_in)
        result = process_font(
            ufo_in,
            tmp_path / "out.otf",
            tmp_path / "out.ufo",
            hint_source="auto",
        )
        assert result.success
        assert result.hint_source_used == "autohint_v2"

    def test_no_hints_in_explicit_source_fails(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        _build_minimal_ufo(ufo_in)
        result = process_font(
            ufo_in,
            tmp_path / "out.otf",
            tmp_path / "out.ufo",
            hint_source="public_ps",
        )
        assert result.success is False
        assert "no hints" in result.error.lower()

    def test_otf_roundtrip(self, tmp_path):
        # 1. UFO → OTF
        ufo_in = tmp_path / "in.ufo"
        otf1 = tmp_path / "stage1.otf"
        ufo1 = tmp_path / "stage1.ufo"
        _build_minimal_ufo(ufo_in)
        r1 = process_font(ufo_in, otf1, ufo1, hint_source="v2")
        assert r1.success, f"stage1 failed: {r1.error}"

        # 2. OTF → OTF (binary input branch; hint_source ignored)
        otf2 = tmp_path / "stage2.otf"
        ufo2 = tmp_path / "stage2.ufo"
        r2 = process_font(otf1, otf2, ufo2)
        assert r2.success, f"stage2 failed: {r2.error}"
        assert r2.hint_source_used == "autohint_v2"
        assert otf2.exists()
        assert ufo2.exists()
