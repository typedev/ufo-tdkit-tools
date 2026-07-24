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


def _build_minimal_ufo(
    ufo_path,
    *,
    with_hints=True,
    with_features=False,
    with_production_names=False,
    hint_glyphs=None,
):
    """Build a tiny UFO with one rectangle glyph; optional v-stem/h-stem hint.

    With ``with_features=True`` the UFO also gets an ``A.alt`` glyph and an
    ``ss01`` feature — real-world UFOs virtually always carry features, and
    the donor-compile step must stay hint-preserving in their presence.

    With ``with_production_names=True`` the UFO also gets an ``Amacron`` glyph
    and a ``public.postscriptNames`` mapping renaming it to ``uni0100`` — the
    setup that used to make the shell and the donor speak different glyph
    namespaces (issue #1).

    ``hint_glyphs`` restricts authored hints to the named glyphs, modelling a
    hand-hinted master where only base forms carry hints.
    """
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

    if with_hints and (hint_glyphs is None or "A" in hint_glyphs):
        # Single v-stem covering the rectangle width (100..500 -> width 400)
        glyph.lib[ADOBE_HINT_KEY_V2] = {
            "hintSetList": [
                {
                    "pointTag": "hintRef0000",
                    "stems": ["vstem 100 400", "hstem 0 700"],
                }
            ],
        }

    if with_production_names:
        amacron = font.newGlyph("Amacron")
        amacron.width = 600
        amacron.unicode = 0x0100
        am_pen = amacron.getPen()
        am_pen.moveTo((100, 0))
        am_pen.lineTo((500, 0))
        am_pen.lineTo((500, 900))
        am_pen.lineTo((100, 900))
        am_pen.closePath()
        amacron[0][0].name = "hintRef0000"
        if with_hints and (hint_glyphs is None or "Amacron" in hint_glyphs):
            amacron.lib[ADOBE_HINT_KEY_V2] = {
                "hintSetList": [
                    {
                        "pointTag": "hintRef0000",
                        "stems": ["vstem 100 400", "hstem 0 900"],
                    }
                ],
            }
        font.lib["public.postscriptNames"] = {"Amacron": "uni0100"}

    if with_features:
        alt = font.newGlyph("A.alt")
        alt.width = 600
        alt_pen = alt.getPen()
        alt_pen.moveTo((100, 0))
        alt_pen.lineTo((500, 0))
        alt_pen.lineTo((500, 650))
        alt_pen.lineTo((100, 650))
        alt_pen.closePath()
        font.features.text = "feature ss01 {\n    sub A by A.alt;\n} ss01;\n"

    font.save(str(ufo_path))


_HINT_OP_NAMES = {"hstem", "vstem", "hstemhm", "vstemhm", "hintmask", "cntrmask"}


def _hint_ops(otf_path, glyph_name):
    """Return the hint operators present in a glyph's CFF charstring."""
    tt = TTFont(str(otf_path))
    charstrings = tt["CFF "].cff.topDictIndex[0].CharStrings
    charstring = charstrings[glyph_name]
    charstring.decompile()
    ops = [op for op in charstring.program if isinstance(op, str) and op in _HINT_OP_NAMES]
    tt.close()
    return ops


def _hinted_glyphs(otf_path):
    """Return the names of glyphs whose charstring carries hints.

    Desubroutinizes first: after ``cffsubr`` the hint operators usually live
    inside a called subroutine, so a top-level program scan undercounts.
    """
    from fontTools import subset

    tt = TTFont(str(otf_path))
    options = subset.Options()
    options.desubroutinize = True
    options.notdef_outline = True
    options.glyph_names = True
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_glyph = True
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(glyphs=tt.getGlyphOrder())
    subsetter.subset(tt)
    charstrings = tt["CFF "].cff.topDictIndex[0].CharStrings
    hinted = set()
    for name in charstrings.keys():
        charstrings[name].decompile()
        if any(
            isinstance(op, str) and op in _HINT_OP_NAMES
            for op in charstrings[name].program
        ):
            hinted.add(name)
    tt.close()
    return hinted


def _cff_widths(otf_path):
    """Return ``{glyph_name: (cff_width, hmtx_width)}`` for every glyph."""
    from fontTools.misc.psCharStrings import T2WidthExtractor

    tt = TTFont(str(otf_path))
    charstrings = tt["CFF "].cff.topDictIndex[0].CharStrings
    widths = {}
    for name in tt.getGlyphOrder():
        charstring = charstrings[name]
        private = charstring.private
        extractor = T2WidthExtractor(
            getattr(private, "Subrs", []),
            charstring.globalSubrs,
            private.nominalWidthX,
            private.defaultWidthX,
        )
        extractor.execute(charstring)
        widths[name] = (extractor.width, tt["hmtx"][name][0])
    tt.close()
    return widths


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

    def test_hints_survive_with_features_present(self, tmp_path):
        """Regression: a UFO WITH a features.fea must still get its authored
        hints into the output CFF.

        The makeotf donor compile used to receive the UFO's features via
        ``-ff``; on some platforms makeotfexe aborts (std::system_error) while
        compiling features from tx-emitted Type 1 input, which made the donor
        compile return False and preserve compilation fail outright. The donor
        is now compiled with NO features file at all (its GSUB/GPOS are
        discarded by the merge anyway), so authored hints must survive — and
        the shell, which does own the features, must still carry GSUB.
        """
        from ufo_tdkit_tools.compilation import compile_otf_preserve_optimized

        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        _build_minimal_ufo(ufo_in, with_features=True)

        stats = {}
        ok = compile_otf_preserve_optimized(str(ufo_in), str(otf_out), stats=stats)

        assert ok, "preserve compilation failed"
        assert stats.get("hints_transferred", 0) >= 1, (
            "no hinted charstrings transferred — donor compile silently failed"
        )
        ops = _hint_ops(otf_out, "A")
        assert ops, "authored hints missing from output charstring"

        tt = TTFont(str(otf_out))
        assert "GSUB" in tt, "shell features (ss01) missing from output"
        tt.close()

    def test_production_renamed_glyphs_keep_hints(self, tmp_path):
        """Regression (issue #1): hints must survive `public.postscriptNames`.

        The ufo2ft shell used to be compiled with production names while the
        `tx -t1` + `makeotf` donor keeps source names, so the per-glyph merge
        skipped every renamed glyph and shipped it unhinted.
        """
        from ufo_tdkit_tools.compilation import compile_otf_preserve_optimized

        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        _build_minimal_ufo(ufo_in, with_production_names=True)

        stats = {}
        ok = compile_otf_preserve_optimized(str(ufo_in), str(otf_out), stats=stats)
        assert ok, "preserve compilation failed"

        tt = TTFont(str(otf_out))
        glyph_order = tt.getGlyphOrder()
        tt.close()
        assert "uni0100" in glyph_order, "production name not applied to output"
        assert "Amacron" not in glyph_order

        hinted = _hinted_glyphs(otf_out)
        assert {"A", "uni0100"} <= hinted, f"renamed glyph lost its hints: {hinted}"

        assert stats.get("name_mismatch") == 0, (
            f"shell and donor disagree on {stats.get('name_mismatch')} glyph name(s)"
        )
        assert stats["hints_transferred"] == stats["donor_hinted"] == 2

    def test_transferred_charstrings_keep_their_width(self, tmp_path):
        """Regression: a merged charstring must not keep the donor's width.

        The width operand is encoded against the donor's
        ``Private.nominalWidthX``, which is not carried into the shell; reusing
        it made every hinted glyph report a bogus advance width in the CFF
        (``tx -dump`` showed 1100 for a 600-unit glyph) while hmtx stayed right.
        """
        from ufo_tdkit_tools.compilation import compile_otf_preserve_optimized

        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        _build_minimal_ufo(ufo_in, with_production_names=True)

        assert compile_otf_preserve_optimized(str(ufo_in), str(otf_out))

        for name, (cff_width, hmtx_width) in _cff_widths(otf_out).items():
            assert cff_width == hmtx_width, (
                f"'{name}': CFF width {cff_width} != hmtx width {hmtx_width}"
            )

    def test_unhinted_ufo_still_gets_production_names(self, tmp_path):
        """The no-hints shortcut path must apply production names too."""
        from ufo_tdkit_tools.compilation import compile_otf_preserve_optimized

        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        _build_minimal_ufo(ufo_in, with_hints=False, with_production_names=True)

        stats = {}
        assert compile_otf_preserve_optimized(str(ufo_in), str(otf_out), stats=stats)
        assert stats["hints_transferred"] == 0

        tt = TTFont(str(otf_out))
        assert "uni0100" in tt.getGlyphOrder()
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

    def test_partially_hinted_source_gets_gaps_filled(self, tmp_path):
        """Regression (issue #1, second defect): the autohint gate was per font.

        `detect_font_source` reports a source as soon as ONE glyph has hints, so
        a hand-hinted master (base forms only) was treated as fully hinted and
        every other glyph shipped unhinted.
        """
        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        _build_minimal_ufo(ufo_in, with_production_names=True, hint_glyphs={"A"})

        result = process_font(
            ufo_in, otf_out, tmp_path / "out.ufo", hint_source="auto", optimize=False
        )

        assert result.success, f"pipeline failed: {result.error}"
        assert result.hint_source_used == "autohint_v2", "authored source must win"
        assert result.glyphs_with_hints == 1
        assert result.autohinted is True
        assert result.autohinted_count == 1, "the unhinted glyph was not filled in"
        assert result.otf_glyphs_hinted == 2

        assert {"A", "uni0100"} <= _hinted_glyphs(otf_out)

    def test_autohint_off_leaves_gaps_unhinted(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        _build_minimal_ufo(ufo_in, with_production_names=True, hint_glyphs={"A"})

        result = process_font(
            ufo_in,
            otf_out,
            tmp_path / "out.ufo",
            hint_source="auto",
            autohint="off",
            optimize=False,
        )

        assert result.success, f"pipeline failed: {result.error}"
        assert result.autohinted is False
        assert result.autohinted_count == 0
        assert result.otf_glyphs_hinted == 1

        hinted = _hinted_glyphs(otf_out)
        assert "A" in hinted and "uni0100" not in hinted

    def test_autohint_all_ignores_authored_hints(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        ufo_out = tmp_path / "out.ufo"
        _build_minimal_ufo(ufo_in, with_production_names=True, hint_glyphs={"A"})

        result = process_font(
            ufo_in, otf_out, ufo_out, hint_source="auto", autohint="all", optimize=False
        )

        assert result.success, f"pipeline failed: {result.error}"
        assert result.hint_source_used == "processedglyphs"
        assert result.autohinted is True
        assert result.autohinted_count == 2, "every drawable glyph should be re-hinted"
        assert result.otf_glyphs_hinted == 2

    def test_autohint_off_with_no_hints_anywhere_fails(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        _build_minimal_ufo(ufo_in, with_hints=False)

        result = process_font(
            ufo_in,
            tmp_path / "out.otf",
            tmp_path / "out.ufo",
            hint_source="auto",
            autohint="off",
        )

        assert result.success is False
        assert "autohint is off" in result.error

    def test_filled_ufo_has_no_leftover_processed_layer(self, tmp_path):
        """The autohint buffer and its hash map must not reach the output UFO."""
        import defcon

        from ufo_tdkit_tools.constants import PROCESSED_LAYER_NAME

        ufo_in = tmp_path / "in.ufo"
        ufo_out = tmp_path / "out.ufo"
        _build_minimal_ufo(ufo_in, with_production_names=True, hint_glyphs={"A"})

        result = process_font(
            ufo_in, tmp_path / "out.otf", ufo_out, hint_source="auto", optimize=False
        )
        assert result.success, f"pipeline failed: {result.error}"

        out = defcon.Font(str(ufo_out))
        assert PROCESSED_LAYER_NAME not in out.layers
        assert not (ufo_out / "data" / "com.adobe.type.processedHashMap").exists()
        # ... and the filled-in hints landed in the default layer where the
        # compiler reads them
        assert out["Amacron"].lib.get(ADOBE_HINT_KEY_V2)

    def test_unhinted_ufo_auto_triggers_autohint(self, tmp_path):
        ufo_in = tmp_path / "in.ufo"
        otf_out = tmp_path / "out.otf"
        ufo_out = tmp_path / "out.ufo"

        _build_minimal_ufo(ufo_in, with_hints=False)
        result = process_font(ufo_in, otf_out, ufo_out, hint_source="auto")

        assert result.success, f"pipeline failed: {result.error}"
        assert result.autohinted is True
        assert result.hint_source_used == "processedglyphs"
        assert otf_out.exists()
        assert ufo_out.exists()

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
