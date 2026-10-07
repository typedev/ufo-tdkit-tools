# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Tests for Glyphs.app source import (.glyphs -> UFO / DesignSpace).

Headless — glyphsLib and fontTools only, no GTK. Fixtures are built by writing
UFOs with ufoLib2 and round-tripping them through `glyphsLib.to_glyphs()`, so
the .glyphs files under test are produced by the same library that reads them
back.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")
ufoLib2 = pytest.importorskip("ufoLib2")

from fontTools.designspaceLib import DesignSpaceDocument  # noqa: E402

from ufo_tdkit_tools.glyphs import (  # noqa: E402
    GLYPHS_EXTENSIONS,
    convert_glyphs_to_ufos,
    default_output_dir,
    describe_existing,
    format_source_summary,
    inspect_glyphs_source,
    is_glyphs_source,
    plan_output_paths,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_ufo(style_name: str, weight_class: int, width: int) -> "ufoLib2.Font":
    """A minimal but complete master: two glyphs, kerning, groups, features."""
    font = ufoLib2.Font()
    font.info.familyName = "Test Family"
    font.info.styleName = style_name
    font.info.unitsPerEm = 1000
    font.info.ascender = 800
    font.info.descender = -200
    font.info.openTypeOS2WeightClass = weight_class

    glyph_a = font.newGlyph("A")
    glyph_a.unicode = 0x0041
    glyph_a.width = width
    pen = glyph_a.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((width, 0))
    pen.lineTo((width, 700))
    pen.closePath()

    glyph_v = font.newGlyph("V")
    glyph_v.unicode = 0x0056
    glyph_v.width = width

    font.groups["public.kern1.A"] = ["A"]
    font.groups["public.kern2.V"] = ["V"]
    font.kerning[("public.kern1.A", "public.kern2.V")] = -40

    font.features.text = "feature liga {\n    sub A V by A;\n} liga;\n"
    return font


def _write_glyphs(tmp_path: Path, ufos: list, name: str = "TestFamily") -> Path:
    """Round-trip UFOs into a .glyphs file on disk."""
    gs_font = glyphsLib.to_glyphs(ufos)
    path = tmp_path / f"{name}.glyphs"
    with open(path, "w", encoding="utf-8") as fp:
        glyphsLib.dump(gs_font, fp)
    return path


@pytest.fixture
def two_master_source(tmp_path: Path) -> Path:
    return _write_glyphs(
        tmp_path,
        [_make_ufo("Regular", 400, 500), _make_ufo("Bold", 700, 600)],
        name="TwoMasters",
    )


@pytest.fixture
def single_master_source(tmp_path: Path) -> Path:
    return _write_glyphs(tmp_path, [_make_ufo("Regular", 400, 500)], name="OneMaster")


# ---------------------------------------------------------------------------
# Source recognition
# ---------------------------------------------------------------------------


def test_extensions_cover_file_and_package():
    assert GLYPHS_EXTENSIONS == (".glyphs", ".glyphspackage")
    assert is_glyphs_source("Family.glyphs")
    assert is_glyphs_source("Family.GLYPHS")  # extension match is case-insensitive
    assert is_glyphs_source(Path("/somewhere/Family.glyphspackage"))
    assert not is_glyphs_source("Family.ufo")
    assert not is_glyphs_source("Family.designspace")


def test_default_output_dir_sits_beside_the_source(tmp_path: Path):
    source = tmp_path / "Family.glyphs"
    assert default_output_dir(source) == tmp_path.resolve() / "Family"


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def test_inspect_reports_masters_and_glyphs(two_master_source: Path):
    info = inspect_glyphs_source(two_master_source)

    assert info.master_count == 2
    assert info.is_multi_master
    assert info.glyph_count == 2
    assert info.family_name == "Test Family"
    assert [tag for _, tag in info.axes]  # at least one axis was derived


def test_inspect_single_master_is_not_multi_master(single_master_source: Path):
    info = inspect_glyphs_source(single_master_source)

    assert info.master_count == 1
    assert not info.is_multi_master


def test_inspect_missing_source_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        inspect_glyphs_source(tmp_path / "nope.glyphs")


def test_format_source_summary_mentions_counts(two_master_source: Path):
    summary = format_source_summary(inspect_glyphs_source(two_master_source))

    assert "2 masters" in summary
    assert "2 glyphs" in summary


# ---------------------------------------------------------------------------
# Planning and collision detection
# ---------------------------------------------------------------------------


def test_plan_lists_every_file_the_conversion_writes(two_master_source: Path, tmp_path: Path):
    out_dir = tmp_path / "out"

    planned = plan_output_paths(two_master_source, out_dir)

    assert len(planned) == 3  # two UFOs + the designspace
    assert all(p.parent == out_dir for p in planned)
    assert sum(1 for p in planned if p.suffix == ".ufo") == 2
    assert planned[-1].name == "TwoMasters.designspace"


def test_plan_matches_what_conversion_actually_writes(two_master_source: Path, tmp_path: Path):
    out_dir = tmp_path / "out"

    planned = set(plan_output_paths(two_master_source, out_dir))
    result = convert_glyphs_to_ufos(two_master_source, out_dir)

    written = set(result.ufo_paths) | {result.designspace_path}
    assert written == {p.resolve() for p in planned}


def test_plan_for_single_master_has_no_designspace(single_master_source: Path, tmp_path: Path):
    planned = plan_output_paths(single_master_source, tmp_path / "out")

    assert len(planned) == 1
    assert planned[0].suffix == ".ufo"


def test_describe_existing_finds_the_collisions(two_master_source: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    planned = plan_output_paths(two_master_source, out_dir)

    assert describe_existing(planned) == []

    convert_glyphs_to_ufos(two_master_source, out_dir)
    assert len(describe_existing(planned)) == 3


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def test_two_masters_produce_a_designspace(two_master_source: Path, tmp_path: Path):
    out_dir = tmp_path / "out"

    result = convert_glyphs_to_ufos(two_master_source, out_dir)

    assert result.is_designspace
    assert result.open_path == result.designspace_path
    assert len(result.ufo_paths) == 2
    assert result.master_count == 2
    assert result.glyph_count == 2
    assert all(p.is_dir() for p in result.ufo_paths)

    doc = DesignSpaceDocument.fromfile(str(result.designspace_path))
    assert len(doc.sources) == 2
    assert doc.axes
    # Sources are referenced by filename relative to the .designspace, so the
    # document stays valid if the whole folder is moved.
    for source in doc.sources:
        assert (out_dir / source.filename).is_dir()


def test_single_master_opens_as_a_plain_ufo(single_master_source: Path, tmp_path: Path):
    out_dir = tmp_path / "out"

    result = convert_glyphs_to_ufos(single_master_source, out_dir)

    assert not result.is_designspace
    assert result.designspace_path is None
    assert result.open_path == result.ufo_paths[0]
    assert result.open_path.suffix == ".ufo"
    assert not list(out_dir.glob("*.designspace"))


def test_kerning_groups_and_features_survive(two_master_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(two_master_source, tmp_path / "out")

    font = ufoLib2.Font.open(result.ufo_paths[0])
    assert ("public.kern1.A", "public.kern2.V") in font.kerning
    assert font.kerning[("public.kern1.A", "public.kern2.V")] == -40
    assert "public.kern1.A" in font.groups
    assert "public.kern2.V" in font.groups
    assert "liga" in (font.features.text or "")


def test_outlines_and_codepoints_survive(two_master_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(two_master_source, tmp_path / "out")

    font = ufoLib2.Font.open(result.ufo_paths[0])
    assert font["A"].unicode == 0x0041
    assert len(font["A"].contours) == 1
    assert font.info.unitsPerEm == 1000


def test_conversion_replaces_a_previous_import(two_master_source: Path, tmp_path: Path):
    out_dir = tmp_path / "out"
    first = convert_glyphs_to_ufos(two_master_source, out_dir)

    # A file that the second run must not leave behind: clean_ufo drops the
    # whole UFO before rewriting it, so a stale glif cannot survive.
    stale = first.ufo_paths[0] / "glyphs" / "Q_.glif"
    stale.write_text("<glyph name='Q' format='2'/>", encoding="utf-8")

    second = convert_glyphs_to_ufos(two_master_source, out_dir)

    assert second.ufo_paths == first.ufo_paths
    assert not stale.exists()


def test_progress_callback_reports_each_master(two_master_source: Path, tmp_path: Path):
    """`(step, total, message)`: a total of 0 means "no count yet".

    Reading the source and building the designspace are single steps inside
    glyphsLib that take minutes on a real family and report nothing, so the
    window has to pulse through them; the write loop is the countable part.
    """
    steps: list[tuple[int, int, str]] = []

    convert_glyphs_to_ufos(
        two_master_source,
        tmp_path / "out",
        progress_callback=lambda step, total, message: steps.append((step, total, message)),
    )

    assert steps  # read + repairs + build + one per master + done
    assert steps[0][:2] == (0, 0), "the first phases have nothing to count"
    assert steps[-1][0] == steps[-1][1] == 2, "the last step is complete"

    counted = [s for s in steps if s[1] > 0]
    assert [s[0] for s in counted] == sorted(s[0] for s in counted), "progress only moves forward"


def test_missing_source_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        convert_glyphs_to_ufos(tmp_path / "nope.glyphs", tmp_path / "out")


def test_output_dir_inside_a_package_is_refused(tmp_path: Path):
    package = tmp_path / "Family.glyphspackage"
    package.mkdir()

    with pytest.raises(ValueError):
        convert_glyphs_to_ufos(package, package / "masters")


def test_output_dir_equal_to_source_is_refused(two_master_source: Path):
    with pytest.raises(ValueError):
        convert_glyphs_to_ufos(two_master_source, two_master_source)


def test_glyphslib_warnings_are_collected(two_master_source: Path, tmp_path: Path, caplog):
    """A glyphsLib warning during conversion becomes a ConversionWarning.

    Emitted through glyphsLib's own logger rather than by provoking a real
    conversion warning, because which sources warn is glyphsLib's business and
    would change under us; what is pinned here is that we are listening.
    """
    import logging

    out_dir = tmp_path / "out"

    original = glyphsLib.to_designspace

    def noisy_to_designspace(*args, **kwargs):
        logging.getLogger("glyphsLib.builder").warning("cannot carry over: something")
        return original(*args, **kwargs)

    glyphsLib.to_designspace = noisy_to_designspace
    try:
        result = convert_glyphs_to_ufos(two_master_source, out_dir)
    finally:
        glyphsLib.to_designspace = original

    assert [w.message for w in result.warnings] == ["cannot carry over: something"]
    assert result.warnings[0].category == "glyphslib"


def test_info_level_glyphslib_chatter_is_not_a_warning(two_master_source: Path, tmp_path: Path):
    """The normal running commentary must not reach the user as warnings."""
    result = convert_glyphs_to_ufos(two_master_source, tmp_path / "out")

    assert result.warnings == []


def test_handler_is_removed_after_conversion(two_master_source: Path, tmp_path: Path):
    import logging

    before = len(logging.getLogger("glyphsLib").handlers)
    convert_glyphs_to_ufos(two_master_source, tmp_path / "out")

    assert len(logging.getLogger("glyphsLib").handlers) == before


def test_handler_is_removed_after_a_failure(tmp_path: Path):
    import logging

    bad = tmp_path / "broken.glyphs"
    bad.write_text("this is not a plist", encoding="utf-8")

    before = len(logging.getLogger("glyphsLib").handlers)
    with pytest.raises(Exception):
        convert_glyphs_to_ufos(bad, tmp_path / "out")

    assert len(logging.getLogger("glyphsLib").handlers) == before
