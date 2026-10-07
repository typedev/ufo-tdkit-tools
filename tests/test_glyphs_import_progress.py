# Copyright 2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Reading a Glyphs source cheaply, cancelling a conversion, and reading the
warnings it produced.

The import used to parse a source three times: once for the dialog's summary,
once to work out which files it would write, and once to convert. On a
216-master family that is 44 s each, and the second one also built the whole
designspace, which costs seven times a parse. These tests pin the cheap path
against the expensive one it replaces.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")
ufoLib2 = pytest.importorskip("ufoLib2")

from ufo_tdkit_tools.glyphs import convert_glyphs_to_ufos, inspect_glyphs_source  # noqa: E402
from ufo_tdkit_tools.glyphs.converter import (  # noqa: E402
    ConversionCancelled,
    _unique_source_filenames,
)
from ufo_tdkit_tools.glyphs.fast_inspect import (  # noqa: E402
    COUNT_UNKNOWN,
    plan_output_filenames,
    quick_inspect,
)
from ufo_tdkit_tools.glyphs.warning_summary import (  # noqa: E402
    summarize_warnings,
    summary_line,
)


def _ufo(style_name: str, width: int) -> "ufoLib2.Font":
    font = ufoLib2.Font()
    font.info.familyName = "Quick Family"
    font.info.styleName = style_name
    font.info.unitsPerEm = 1000
    font.info.ascender = 800
    font.info.descender = -200
    glyph = font.newGlyph("A")
    glyph.unicode = 0x0041
    glyph.width = width
    pen = glyph.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((width, 0))
    pen.lineTo((width, 700))
    pen.closePath()
    return font


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    gs = glyphsLib.to_glyphs([_ufo("Regular", 500), _ufo("Bold", 600), _ufo("Black", 700)])
    path = tmp_path / "QuickFamily.glyphs"
    with open(path, "w", encoding="utf-8") as fp:
        glyphsLib.dump(gs, fp)
    return path


@pytest.fixture
def source_package(source_file: Path, tmp_path: Path) -> Path:
    """The same font as a .glyphspackage: fontinfo.plist plus one file a glyph."""
    import openstep_plist

    with source_file.open(encoding="utf-8") as fp:
        data = openstep_plist.load(fp, use_numbers=True)

    package = tmp_path / "QuickFamily.glyphspackage"
    (package / "glyphs").mkdir(parents=True)
    glyphs = data.pop("glyphs", [])
    with (package / "fontinfo.plist").open("w", encoding="utf-8") as fp:
        openstep_plist.dump(data, fp)
    names = []
    for glyph in glyphs:
        name = glyph.get("glyphname", "?")
        names.append(name)
        with (package / "glyphs" / f"{name}_.glyph").open("w", encoding="utf-8") as fp:
            openstep_plist.dump(glyph, fp)
    with (package / "order.plist").open("w", encoding="utf-8") as fp:
        openstep_plist.dump(names, fp)
    return package


# ---------------------------------------------------------------------------
# The cheap read agrees with the expensive one
# ---------------------------------------------------------------------------


def test_quick_inspect_agrees_with_the_full_parse(source_file: Path):
    quick = quick_inspect(source_file)
    full = inspect_glyphs_source(source_file)

    assert quick.family_name == full.family_name
    assert quick.master_count == full.master_count
    assert quick.glyph_count == full.glyph_count
    assert quick.instance_count == full.instance_count
    assert quick.axes == full.axes


def test_quick_inspect_reads_a_package_without_its_glyphs(source_package: Path):
    quick = quick_inspect(source_package)

    assert quick.master_count == 3
    assert quick.glyph_count == 1
    assert quick.family_name == "Quick Family"


def test_hint_counts_are_declared_unknown_rather_than_guessed(source_file: Path):
    """They need the glyphs. Saying "unknown" beats printing a wrong zero."""
    quick = quick_inspect(source_file)

    assert quick.ps_hint_count == COUNT_UNKNOWN
    assert quick.tt_hint_count == COUNT_UNKNOWN


def test_missing_source_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        quick_inspect(tmp_path / "nope.glyphs")


def test_an_unknown_count_is_not_shown_as_a_number(source_file: Path):
    """ "-1 PS hints" is what printing COUNT_UNKNOWN looked like."""
    from ufo_tdkit_tools.glyphs import format_source_summary

    line = format_source_summary(quick_inspect(source_file))

    assert "-1" not in line
    assert "PS hints" not in line


def test_corner_parts_seen_in_the_names_count_as_present():
    """The switch has to appear; the cheap read only misses *how many*."""
    from ufo_tdkit_tools.glyphs.converter import GlyphsSourceInfo

    def info(corners: int) -> GlyphsSourceInfo:
        return GlyphsSourceInfo(
            path=Path("x.glyphs"),
            family_name="X",
            master_count=1,
            glyph_count=1,
            instance_count=0,
            corner_component_count=corners,
        )

    unknown = info(COUNT_UNKNOWN)
    none_at_all = info(0)

    assert unknown.has_corner_components
    assert not none_at_all.has_corner_components


# ---------------------------------------------------------------------------
# Filenames, without building a designspace
# ---------------------------------------------------------------------------


def test_predicted_filenames_match_the_conversion(source_file: Path):
    """The dialog's "these will be replaced" list has to be the real one."""
    from glyphsLib import to_designspace

    font = glyphsLib.load(str(source_file))
    predicted = plan_output_filenames(font)
    real = _unique_source_filenames(to_designspace(font, write_skipexportglyphs=True))

    assert predicted == real


def test_same_named_masters_get_distinct_filenames(source_file: Path):
    font = glyphsLib.load(str(source_file))
    for master in font.masters:
        master.name = "Regular"

    names = plan_output_filenames(font)

    assert len(set(names)) == len(names), f"filenames collided: {names}"


# ---------------------------------------------------------------------------
# Reusing a parse
# ---------------------------------------------------------------------------


def test_conversion_can_take_an_already_parsed_font(source_file: Path, tmp_path: Path, monkeypatch):
    font = glyphsLib.load(str(source_file))

    def refuse(*args, **kwargs):  # pragma: no cover - called only on failure
        raise AssertionError("the source was parsed again")

    monkeypatch.setattr(glyphsLib, "load", refuse)
    result = convert_glyphs_to_ufos(source_file, tmp_path / "out", font=font)

    assert len(result.ufo_paths) == 3


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class _CancelAfter:
    """A cancel flag that trips once the conversion has written some masters."""

    def __init__(self, after: int):
        self._after = after
        self.written = 0

    def note(self, step: int, total: int, message: str) -> None:
        if total > 0 and message.startswith(("Writing", "Replacing")):
            self.written = step

    def is_set(self) -> bool:
        return self.written >= self._after


def test_cancelling_removes_what_this_run_wrote(source_file: Path, tmp_path: Path):
    out_dir = tmp_path / "fresh"
    flag = _CancelAfter(1)

    with pytest.raises(ConversionCancelled) as excinfo:
        convert_glyphs_to_ufos(source_file, out_dir, progress_callback=flag.note, cancel=flag)

    assert excinfo.value.removed, "the partial masters are reported"
    assert not list(out_dir.glob("*.ufo")), "no half-written master is left behind"
    assert not out_dir.exists(), "a folder this run created is removed with it"


def test_cancelling_leaves_a_folder_the_user_already_had(source_file: Path, tmp_path: Path):
    out_dir = tmp_path / "existing"
    out_dir.mkdir()
    keeper = out_dir / "notes.txt"
    keeper.write_text("mine", encoding="utf-8")
    flag = _CancelAfter(1)

    with pytest.raises(ConversionCancelled):
        convert_glyphs_to_ufos(source_file, out_dir, progress_callback=flag.note, cancel=flag)

    assert out_dir.is_dir()
    assert keeper.read_text(encoding="utf-8") == "mine"


def test_cancelling_before_anything_is_written(source_file: Path, tmp_path: Path):
    class Always:
        def is_set(self):
            return True

    with pytest.raises(ConversionCancelled):
        convert_glyphs_to_ufos(source_file, tmp_path / "out", cancel=Always())

    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# Warnings, grouped
# ---------------------------------------------------------------------------


def _warning(message: str, category: str = "glyphsLib", details: str = ""):
    from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

    return ConversionWarning(
        category=category, severity=WarningSeverity.WARNING, message=message, details=details
    )


def test_a_thousand_of_the_same_sentence_become_one_row():
    warnings = [_warning(f"Glyph 'g{i}': component 'x{i}' was decomposed") for i in range(1000)]

    groups = summarize_warnings(warnings)

    assert len(groups) == 1
    assert groups[0].count == 1000
    assert len(groups[0].examples) == 50, "a few examples, not a thousand"


def test_repairs_are_listed_before_observations():
    warnings = [_warning("Glyph 'a': decomposed") for _ in range(10)]
    warnings.append(_warning("Glyph 'b': renamed a layer", category="preflight"))

    groups = summarize_warnings(warnings)

    assert groups[0].is_repair, "what the import changed comes first"
    assert groups[0].count == 1
    assert groups[1].count == 10


def test_an_unquoted_class_name_does_not_split_a_group():
    """glyphsLib names kerning classes without quoting them."""
    groups = summarize_warnings(
        [
            _warning("Non-existent glyph class public.kern1.o_left found in kerning rules."),
            _warning("Non-existent glyph class public.kern2.Yhook_right found in kerning rules."),
        ]
    )

    assert len(groups) == 1
    assert groups[0].template.endswith("found in kerning rules."), groups[0].template


def test_two_genuinely_different_sentences_stay_apart():
    groups = summarize_warnings(
        [
            _warning("Non-existent glyph class public.kern1.o_left found in kerning rules."),
            _warning("Glyph 'a': component 'x' was decomposed"),
        ]
    )

    assert len(groups) == 2


def test_numbers_do_not_split_a_group():
    groups = summarize_warnings([_warning("Removed 3 points"), _warning("Removed 17 points")])

    assert len(groups) == 1


def test_summary_line_is_one_line():
    line = summary_line(summarize_warnings([_warning("Glyph 'a': decomposed")] * 5))

    assert "5 warnings" in line
    assert "\n" not in line


def test_summary_line_for_a_clean_import():
    assert summary_line([]) == "no warnings"
