# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for the ufo-tdkit-tools CLI."""

from __future__ import annotations

import shutil

import pytest

from ufo_tdkit_tools.cli import _build_parser, main


class TestArgumentParsing:
    def test_no_command_prints_help_and_returns_2(self, capsys):
        assert main([]) == 2
        out = capsys.readouterr().out
        assert "optimize-otf" in out

    def test_in_place_parsed(self):
        ns = _build_parser().parse_args(["optimize-otf", "--in-place", "a.otf", "b.otf"])
        assert ns.in_place is True
        assert ns.files == ["a.otf", "b.otf"]
        assert ns.optimize is True  # default on

    def test_no_optimize_toggle(self):
        ns = _build_parser().parse_args(["optimize-otf", "--no-optimize", "-o", "out", "a.otf"])
        assert ns.optimize is False
        assert ns.output_dir == "out"

    def test_in_place_and_output_dir_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["optimize-otf", "--in-place", "-o", "out", "a.otf"])

    def test_output_target_required(self):
        # neither --in-place nor -o
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["optimize-otf", "a.otf"])

    def test_hint_source_choices(self):
        ns = _build_parser().parse_args(
            ["optimize-otf", "--hint-source", "v2", "-o", "out", "a.ufo"]
        )
        assert ns.hint_source == "v2"
        with pytest.raises(SystemExit):
            _build_parser().parse_args(
                ["optimize-otf", "--hint-source", "bogus", "-o", "out", "a.ufo"]
            )


class TestSummaryAndExitCodes:
    def test_missing_file_reports_failure(self, tmp_path, capsys):
        out_dir = tmp_path / "out"
        rc = main(["optimize-otf", "-o", str(out_dir), str(tmp_path / "nope.otf")])
        assert rc == 1
        summary = capsys.readouterr().out.strip().splitlines()[-1]
        assert summary == "optimized=0 autohinted=0 failed=1"

    def test_inplace_rejects_non_otf_binary(self, tmp_path, capsys):
        ttf = tmp_path / "x.ttf"
        ttf.write_bytes(b"\x00\x01\x00\x00")  # content irrelevant; rejected on suffix
        rc = main(["optimize-otf", "--in-place", str(ttf)])
        assert rc == 1
        summary = capsys.readouterr().out.strip().splitlines()[-1]
        assert summary == "optimized=0 autohinted=0 failed=1"


# ── End-to-end (requires afdko tx/makeotf) ───────────────────────────────────


@pytest.mark.skipif(
    shutil.which("tx") is None or shutil.which("makeotf") is None,
    reason="afdko tx/makeotf not on PATH",
)
def test_inplace_roundtrip_smoke(tmp_path, capsys):
    pytest.importorskip("afdko")
    pytest.importorskip("ufo2ft")
    defcon = pytest.importorskip("defcon")
    from ufo2ft import compileOTF

    font = defcon.Font()
    info = font.info
    info.familyName = "CliSmoke"
    info.styleName = "Regular"
    info.postscriptFontName = "CliSmoke-Regular"
    info.unitsPerEm = 1000
    info.ascender = 750
    info.descender = -250
    info.openTypeOS2Selection = [7]  # USE_TYPO_METRICS
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

    otf = tmp_path / "Font.otf"
    compileOTF(defcon.Font(str(src_ufo)), optimizeCFF=1).save(str(otf))
    size_before = otf.stat().st_size

    rc = main(["optimize-otf", "--in-place", "--no-optimize", str(otf)])
    assert rc == 0
    summary = capsys.readouterr().out.strip().splitlines()[-1]
    assert summary.startswith("optimized=1")
    assert otf.exists() and otf.stat().st_size > 0
    # File was rewritten in place, USE_TYPO_METRICS still present.
    from fontTools.ttLib import TTFont

    f = TTFont(str(otf))
    assert bool(f["OS/2"].fsSelection & (1 << 7)), "USE_TYPO_METRICS must survive CLI in-place"
    f.close()
    assert size_before  # sanity
