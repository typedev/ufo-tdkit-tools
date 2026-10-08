# Copyright 2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for the ``glyphs2ufo`` CLI command."""

from __future__ import annotations

import _thread
import time
from pathlib import Path

import pytest

from ufo_tdkit_tools.cli import _build_parser, main

glyphsLib = pytest.importorskip("glyphsLib")
ufoLib2 = pytest.importorskip("ufoLib2")


def _make_ufo(style_name: str, weight_class: int) -> "ufoLib2.Font":
    font = ufoLib2.Font()
    font.info.familyName = "Test Family"
    font.info.styleName = style_name
    font.info.unitsPerEm = 1000
    font.info.openTypeOS2WeightClass = weight_class
    glyph = font.newGlyph("A")
    glyph.unicode = 0x0041
    glyph.width = 500
    pen = glyph.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((500, 0))
    pen.lineTo((500, 700))
    pen.closePath()
    return font


def _write_glyphs(directory: Path, styles: list[tuple[str, int]], name: str) -> Path:
    path = directory / f"{name}.glyphs"
    with open(path, "w", encoding="utf-8") as fp:
        glyphsLib.dump(glyphsLib.to_glyphs([_make_ufo(s, w) for s, w in styles]), fp)
    return path


@pytest.fixture
def two_masters(tmp_path: Path) -> Path:
    return _write_glyphs(tmp_path, [("Regular", 400), ("Bold", 700)], "TwoMasters")


@pytest.fixture
def one_master(tmp_path: Path) -> Path:
    return _write_glyphs(tmp_path, [("Regular", 400)], "OneMaster")


class TestArgumentParsing:
    def test_defaults(self):
        args = _build_parser().parse_args(["glyphs2ufo", "A.glyphs"])
        assert args.sources == ["A.glyphs"]
        assert args.output_dir is None
        assert args.apply_corners  # a finished UFO has its corners baked
        assert not (args.force or args.dry_run)

    def test_keep_corners_opts_out(self):
        assert (
            not _build_parser()
            .parse_args(["glyphs2ufo", "--keep-corners", "A.glyphs"])
            .apply_corners
        )

    def test_apply_corners_still_accepted(self):
        assert (
            _build_parser().parse_args(["glyphs2ufo", "--apply-corners", "A.glyphs"]).apply_corners
        )

    def test_corner_flags_are_exclusive(self):
        with pytest.raises(SystemExit):
            _build_parser().parse_args(
                ["glyphs2ufo", "--keep-corners", "--apply-corners", "A.glyphs"]
            )

    def test_verbose_and_quiet_are_exclusive(self):
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["glyphs2ufo", "-v", "-q", "A.glyphs"])


class TestConversion:
    def test_default_output_beside_source(self, two_masters, capsys):
        assert main(["glyphs2ufo", str(two_masters)]) == 0
        out_dir = two_masters.parent / "TwoMasters"
        assert (out_dir / "TwoMasters.designspace").is_file()
        assert len(list(out_dir.glob("*.ufo"))) == 2
        assert capsys.readouterr().out.strip() == "converted=1 failed=0"

    def test_single_master_writes_no_designspace(self, one_master):
        assert main(["glyphs2ufo", "-q", str(one_master)]) == 0
        out_dir = one_master.parent / "OneMaster"
        assert [p.suffix for p in out_dir.iterdir()] == [".ufo"]

    def test_existing_outputs_need_force(self, two_masters, capsys, caplog):
        assert main(["glyphs2ufo", "-q", str(two_masters)]) == 0
        capsys.readouterr()

        assert main(["glyphs2ufo", "-q", str(two_masters)]) == 1
        assert capsys.readouterr().out.strip() == "converted=0 failed=1"
        assert "--force" in caplog.text

        assert main(["glyphs2ufo", "-q", "--force", str(two_masters)]) == 0

    def test_output_dir_with_several_sources_uses_subdirs(self, two_masters, one_master, tmp_path):
        out = tmp_path / "out"
        assert main(["glyphs2ufo", "-q", "-o", str(out), str(two_masters), str(one_master)]) == 0
        assert sorted(p.name for p in out.iterdir()) == ["OneMaster", "TwoMasters"]

    def test_output_dir_with_one_source_is_used_as_is(self, one_master, tmp_path):
        out = tmp_path / "exact"
        assert main(["glyphs2ufo", "-q", "-o", str(out), str(one_master)]) == 0
        assert [p.suffix for p in out.iterdir()] == [".ufo"]

    def test_dry_run_writes_nothing(self, two_masters, capsys):
        assert main(["glyphs2ufo", "--dry-run", str(two_masters)]) == 0
        out = capsys.readouterr().out
        assert "TwoMasters.designspace" in out
        assert out.strip().endswith("planned=1 failed=0")
        assert not (two_masters.parent / "TwoMasters").exists()

    def test_bad_inputs_fail_without_aborting_the_batch(self, one_master, tmp_path, capsys):
        missing = tmp_path / "missing.glyphs"
        not_glyphs = tmp_path / "font.ufo"
        assert main(["glyphs2ufo", "-q", str(missing), str(not_glyphs), str(one_master)]) == 1
        assert capsys.readouterr().out.strip() == "converted=1 failed=2"


class TestCorners:
    @pytest.fixture
    def corner_source(self, tmp_path: Path) -> Path:
        from tests.test_glyphs_corners_hints import _CORNER_SOURCE

        path = tmp_path / "CornerTest.glyphs"
        path.write_text(_CORNER_SOURCE, encoding="utf-8")
        return path

    def _points(self, out_dir: Path) -> int:
        (ufo,) = out_dir.glob("*.ufo")
        return len(ufoLib2.Font.open(ufo)["A"].contours[0].points)

    def test_corners_are_baked_by_default(self, corner_source, tmp_path):
        assert main(["glyphs2ufo", "-q", "-o", str(tmp_path / "out"), str(corner_source)]) == 0
        assert self._points(tmp_path / "out") > 4

    def test_keep_corners_leaves_the_outline_alone(self, corner_source, tmp_path):
        out = tmp_path / "out"
        assert main(["glyphs2ufo", "-q", "--keep-corners", "-o", str(out), str(corner_source)]) == 0
        assert self._points(out) == 4


class TestCancel:
    def test_ctrl_c_cancels_cleanly(self, two_masters, monkeypatch, capsys, caplog):
        from ufo_tdkit_tools.glyphs import converter

        def fake_convert(source, out_dir, progress_callback=None, apply_corners=False, cancel=None):
            time.sleep(0.3)  # let the CLI reach its join loop
            _thread.interrupt_main()  # what Ctrl-C does to the main thread
            deadline = time.monotonic() + 5
            while not cancel.is_set():
                assert time.monotonic() < deadline, "cancel never set"
                time.sleep(0.01)
            raise converter.ConversionCancelled(removed=[Path(out_dir) / "x.ufo"])

        monkeypatch.setattr(converter, "convert_glyphs_to_ufos", fake_convert)
        assert main(["glyphs2ufo", "-q", str(two_masters)]) == 130
        assert "cancelled=1" in capsys.readouterr().out
        assert "removed 1 path(s)" in caplog.text
