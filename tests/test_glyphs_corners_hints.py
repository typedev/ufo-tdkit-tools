# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Tests for the two extra passes of a Glyphs import.

Corner components are applied only on request — they rewrite outlines and cannot
be undone. Hand-made PostScript hints are carried across unconditionally, the way
the binary importer carries CFF hints: it adds a lib key and touches no outline.

Headless — glyphsLib and ufoLib2 only, no GTK.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")
ufoLib2 = pytest.importorskip("ufoLib2")

from ufo_tdkit_tools.glyphs import convert_glyphs_to_ufos  # noqa: E402
from ufo_tdkit_tools.glyphs.corners import (  # noqa: E402
    HINTS_LIB_KEY,
    UFO2FT_FILTERS_KEY,
    apply_corner_components,
)
from ufo_tdkit_tools.glyphs.ps_hints import (  # noqa: E402
    ADOBE_HINT_KEY_V2,
    glyphs_hints_to_stems,
    import_ps_hints,
)

# ---------------------------------------------------------------------------
# Corner components
# ---------------------------------------------------------------------------

_CORNER_SOURCE = """{
.appVersion = "1361";
familyName = CornerTest;
fontMaster = (
{
ascender = 800;
descender = -200;
id = m01;
weightValue = 400;
}
);
glyphs = (
{
glyphname = "_corner.round";
layers = (
{
layerId = m01;
paths = (
{
closed = 0;
nodes = (
"0 100 LINE",
"0 40 OFFCURVE",
"40 0 OFFCURVE",
"100 0 CURVE"
);
}
);
width = 100;
}
);
},
{
glyphname = A;
layers = (
{
hints = (
{
name = "_corner.round";
origin = "{0, 1}";
type = Corner;
}
);
layerId = m01;
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"400 0 LINE",
"400 700 LINE",
"0 700 LINE"
);
}
);
width = 400;
}
);
unicode = 0041;
}
);
unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
}
"""


@pytest.fixture
def corner_source(tmp_path: Path) -> Path:
    path = tmp_path / "CornerTest.glyphs"
    path.write_text(_CORNER_SOURCE, encoding="utf-8")
    return path


def test_corners_are_not_applied_by_default(corner_source: Path, tmp_path: Path):
    """Import leaves the outline as the source stores it unless asked."""
    result = convert_glyphs_to_ufos(corner_source, tmp_path / "out")

    font = ufoLib2.Font.open(result.open_path)
    assert len(font["A"].contours[0].points) == 4  # still a plain rectangle
    assert result.corners_applied == 0
    assert any(f["name"] == "cornerComponents" for f in font.lib[UFO2FT_FILTERS_KEY])


def test_corners_are_applied_when_asked(corner_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(corner_source, tmp_path / "out", apply_corners=True)

    font = ufoLib2.Font.open(result.open_path)
    points = font["A"].contours[0].points
    assert len(points) == 7  # the corner became a real curve
    assert result.corners_applied == 1
    assert any(p.type == "curve" for p in points)


def test_baking_disarms_the_compile_time_filter(corner_source: Path, tmp_path: Path):
    """Otherwise ufo2ft would apply every corner a second time on export."""
    result = convert_glyphs_to_ufos(corner_source, tmp_path / "out", apply_corners=True)

    font = ufoLib2.Font.open(result.open_path)
    remaining = [f["name"] for f in font.lib.get(UFO2FT_FILTERS_KEY, [])]
    assert "cornerComponents" not in remaining
    assert "eraseOpenCorners" in remaining  # a different pass, still wanted


def test_baking_removes_the_corner_hints_only(corner_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(corner_source, tmp_path / "out", apply_corners=True)

    font = ufoLib2.Font.open(result.open_path)
    assert HINTS_LIB_KEY not in font["A"].lib


def test_a_second_bake_changes_nothing(corner_source: Path, tmp_path: Path):
    """The filter itself is not idempotent — the cleanup is what makes it so."""
    result = convert_glyphs_to_ufos(corner_source, tmp_path / "out", apply_corners=True)
    font = ufoLib2.Font.open(result.open_path)
    before = len(font["A"].contours[0].points)

    applied = apply_corner_components(font)

    assert applied == 0
    assert len(font["A"].contours[0].points) == before


def test_baking_a_font_without_corners_is_a_no_op(tmp_path: Path):
    font = ufoLib2.Font()
    glyph = font.newGlyph("A")
    pen = glyph.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((100, 0))
    pen.lineTo((100, 100))
    pen.closePath()

    assert apply_corner_components(font) == 0


# ---------------------------------------------------------------------------
# PostScript hints
# ---------------------------------------------------------------------------


def _hinted_glyph(hints: list[dict]) -> "ufoLib2.objects.Glyph":
    font = ufoLib2.Font()
    glyph = font.newGlyph("A")
    pen = glyph.getPen()
    pen.moveTo((0, 0))
    pen.lineTo((400, 0))
    pen.lineTo((400, 700))
    pen.lineTo((0, 700))
    pen.closePath()
    glyph.lib[HINTS_LIB_KEY] = hints
    return font, glyph


def test_horizontal_hint_becomes_hstem():
    """A Glyphs horizontal hint constrains Y, which is what hstem means."""
    _, glyph = _hinted_glyph([{"type": "Stem", "horizontal": True, "place": [261, 140]}])

    assert glyphs_hints_to_stems(glyph) == ["hstem 261 140"]


def test_vertical_hint_becomes_vstem():
    _, glyph = _hinted_glyph([{"type": "Stem", "horizontal": False, "place": [276, 179]}])

    assert glyphs_hints_to_stems(glyph) == ["vstem 276 179"]


def _stepped_glyph(hints: list[dict], closed: bool = True):
    """A contour whose points all differ in Y, so a wrong index cannot pass.

    The square used elsewhere has pairs of points sharing a Y, which makes an
    off-by-one in node indexing invisible — the bug this shape exists to catch
    survived exactly such a test.
    """
    font = ufoLib2.Font()
    glyph = font.newGlyph("A")
    pen = glyph.getPen()
    pen.moveTo((0, 100))
    pen.lineTo((100, 200))
    pen.lineTo((200, 300))
    pen.lineTo((300, 400))
    if closed:
        pen.closePath()
    else:
        pen.endPath()
    glyph.lib[HINTS_LIB_KEY] = hints
    return font, glyph


def test_ghosts_take_their_position_from_the_node_they_hang_on():
    """A ghost carries no `place` — it names a node, and Adobe marks it by width."""
    _, glyph = _hinted_glyph(
        [
            # point [0][2] is (400, 700); point [0][0] is (0, 0)
            {"type": "TopGhost", "horizontal": True, "origin": [0, 2]},
            {"type": "BottomGhost", "horizontal": True, "origin": [0, 0]},
        ]
    )

    assert glyphs_hints_to_stems(glyph) == ["hstem 700 -20", "hstem 0 -21"]


def test_a_closed_contour_is_rotated_by_one_on_the_way_into_a_ufo():
    """Glyphs keeps a closed contour's start node LAST; glyphsLib rotates it first.

    Reading a Glyphs node index straight out of the UFO therefore lands on the
    previous node. Nothing reports that — the hint simply attaches to the wrong
    place — and on a real hinted master it was every single ghost.
    """
    _, glyph = _stepped_glyph(
        [
            {"type": "BottomGhost", "horizontal": True, "origin": [0, 0]},
            {"type": "TopGhost", "horizontal": True, "origin": [0, 3]},
        ]
    )

    # Glyphs node 0 is the UFO's *second* point (y=200); node 3 wraps to the
    # first (y=100). Without the rotation these would read 100 and 400.
    assert glyphs_hints_to_stems(glyph) == ["hstem 200 -21", "hstem 100 -20"]


def test_an_open_contour_is_not_rotated():
    """Its first node becomes the `move`, so the indices already line up."""
    _, glyph = _stepped_glyph(
        [
            {"type": "BottomGhost", "horizontal": True, "origin": [0, 0]},
            {"type": "TopGhost", "horizontal": True, "origin": [0, 3]},
        ],
        closed=False,
    )

    assert glyphs_hints_to_stems(glyph) == ["hstem 100 -21", "hstem 400 -20"]


def test_a_vertical_ghost_reads_x_not_y():
    _, glyph = _stepped_glyph([{"type": "TopGhost", "horizontal": False, "origin": [0, 0]}])

    assert glyphs_hints_to_stems(glyph) == ["vstem 100 -20"]


def test_truetype_and_component_hints_are_skipped():
    _, glyph = _hinted_glyph(
        [
            {"type": "TTSnap", "horizontal": True, "origin": [0, 1]},
            {"type": "TTStem", "horizontal": True, "origin": [0, 1], "target": [0, 2]},
            {"type": "Corner", "name": "_corner.round", "origin": [0, 1]},
            {"type": "Stem", "horizontal": False, "place": [40, 148]},
        ]
    )

    assert glyphs_hints_to_stems(glyph) == ["vstem 40 148"]


def test_a_glyph_without_hints_yields_nothing():
    _, glyph = _hinted_glyph([])

    assert glyphs_hints_to_stems(glyph) == []


def test_incomplete_hints_are_dropped_rather_than_guessed():
    _, glyph = _hinted_glyph(
        [
            {"type": "Stem", "horizontal": True},  # no place
            {"type": "TopGhost", "horizontal": True},  # no origin
            {"type": "TopGhost", "horizontal": True, "origin": [9, 9]},  # nowhere
            {"type": "Stem", "horizontal": True, "place": [100, 20]},
        ]
    )

    assert glyphs_hints_to_stems(glyph) == ["hstem 100 20"]


def test_numbers_are_written_as_integers_where_possible():
    _, glyph = _hinted_glyph([{"type": "Stem", "horizontal": True, "place": [261.0, 140.5]}])

    assert glyphs_hints_to_stems(glyph) == ["hstem 261 140.5"]


def test_import_writes_the_adobe_key_and_anchors_it():
    font, glyph = _hinted_glyph([{"type": "Stem", "horizontal": True, "place": [261, 140]}])

    written = import_ps_hints(font)

    assert written == 1
    data = glyph.lib[ADOBE_HINT_KEY_V2]
    assert data["formatVersion"] == "1"
    assert data["id"] == ""  # empty disables the staleness check
    assert len(data["hintSetList"]) == 1
    hint_set = data["hintSetList"][0]
    assert hint_set["stems"] == ["hstem 261 140"]
    # the anchor must name a point that actually exists in the outline
    names = {p.name for c in glyph.contours for p in c.points}
    assert hint_set["pointTag"] in names


def test_import_skips_glyphs_with_no_postscript_hints():
    font, glyph = _hinted_glyph([{"type": "TTSnap", "horizontal": True, "origin": [0, 1]}])

    assert import_ps_hints(font) == 0
    assert ADOBE_HINT_KEY_V2 not in glyph.lib


def test_written_hints_parse_with_the_overlays_own_reader():
    """The format is only right if the code that draws it accepts it."""
    parser = pytest.importorskip("ufo_tdkit_tools.ps_hints.parser")

    font, glyph = _hinted_glyph(
        [
            {"type": "Stem", "horizontal": True, "place": [261, 140]},
            {"type": "Stem", "horizontal": False, "place": [276, 179]},
            {"type": "TopGhost", "horizontal": True, "origin": [0, 2]},
        ]
    )
    import_ps_hints(font)

    data = parser.parse_ps_hints(glyph, parser.HintSource.AUTOHINT_V2, font=font)

    assert data.errors == []
    assert len(data.hint_sets) == 1


def test_hints_come_across_without_being_asked_for(tmp_path: Path):
    source = tmp_path / "Hinted.glyphs"
    source.write_text(
        _CORNER_SOURCE.replace(
            """hints = (
{
name = "_corner.round";
origin = "{0, 1}";
type = Corner;
}
);""",
            """hints = (
{
horizontal = 1;
place = "{261, 140}";
type = Stem;
}
);""",
        ),
        encoding="utf-8",
    )

    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    assert result.ps_hints_imported == 1
    font = ufoLib2.Font.open(result.open_path)
    assert font["A"].lib[ADOBE_HINT_KEY_V2]["hintSetList"][0]["stems"] == ["hstem 261 140"]


def test_a_source_without_postscript_hints_gets_no_hint_data(tmp_path: Path):
    """The unconditional pass must stay silent when there is nothing to carry."""
    source = tmp_path / "Unhinted.glyphs"
    source.write_text(_CORNER_SOURCE, encoding="utf-8")  # carries a Corner hint only

    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    assert result.ps_hints_imported == 0
    font = ufoLib2.Font.open(result.open_path)
    assert ADOBE_HINT_KEY_V2 not in font["A"].lib


_GHOSTS = """type = Corner;
},
{
horizontal = 1;
origin = "{0, 2}";
type = TopGhost;
},
{
horizontal = 1;
origin = "{0, 0}";
type = BottomGhost;
}
);"""


@pytest.mark.parametrize("corner_node", ["{0, 1}", "{0, 3}"])
def test_ghost_hints_survive_baking_corners(tmp_path: Path, corner_node: str):
    """Ghosts name their node by index and baking inserts points, so the hints
    must be read before the bake (a top ghost at 700 came out at 0). With the
    corner on the contour's first node ({0, 3} in Glyphs order), the point the
    hint set hangs on is rebuilt too and must be re-anchored."""
    source = tmp_path / "Ghosts.glyphs"
    source.write_text(
        _CORNER_SOURCE.replace('origin = "{0, 1}";', f'origin = "{corner_node}";').replace(
            "type = Corner;\n}\n);", _GHOSTS
        ),
        encoding="utf-8",
    )

    result = convert_glyphs_to_ufos(source, tmp_path / "out", apply_corners=True)

    assert result.corners_applied == 1
    glyph = ufoLib2.Font.open(result.open_path)["A"]
    hint_set = glyph.lib[ADOBE_HINT_KEY_V2]["hintSetList"][0]
    assert hint_set["stems"] == ["hstem 700 -20", "hstem 0 -21"]
    names = {p.name for c in glyph.contours for p in c.points if p.name}
    assert hint_set["pointTag"] in names
