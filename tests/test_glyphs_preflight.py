# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Tests for the repairs applied to a Glyphs source before conversion.

Headless — glyphsLib only, no GTK. Each fixture reproduces a defect found in a
real production source, every one of which aborted the import.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")

from ufo_tdkit_tools.glyphs import convert_glyphs_to_ufos  # noqa: E402
from ufo_tdkit_tools.glyphs.preflight import (  # noqa: E402
    UnwritableOutlineError,
    count_component_hints,
    count_hints_by_family,
    preflight_source,
)


# ---------------------------------------------------------------------------
# Fixtures — written as Glyphs 2 plists, the shape the real sources have
# ---------------------------------------------------------------------------

_HEADER = """{
.appVersion = "1361";
familyName = PreflightTest;
"""

_MASTER = """fontMaster = (
{
ascender = 800;
descender = -200;
id = m01;
weightValue = 400;
}
);
"""

_FOOTER = """unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
}
"""


def _square(name: str, unicode_hex: str | None = None, extra: str = "") -> str:
    uni = f"unicode = {unicode_hex};\n" if unicode_hex else ""
    return f"""{{
glyphname = {name};
layers = (
{{
layerId = m01;
{extra}paths = (
{{
closed = 1;
nodes = (
"0 0 LINE",
"400 0 LINE",
"400 700 LINE",
"0 700 LINE"
);
}}
);
width = 400;
}}
);
{uni}}},
"""


def _write(tmp_path: Path, body: str, name: str = "Test") -> Path:
    path = tmp_path / f"{name}.glyphs"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def duplicate_parameter_source(tmp_path: Path) -> Path:
    """The defect that stopped two of three real sources: fsType written twice."""
    body = (
        _HEADER
        + """customParameters = (
{
name = fsType;
value = (
2
);
},
{
name = fsType;
value = (
3
);
}
);
"""
        + _MASTER
        + "glyphs = (\n"
        + _square("A", "0041")
        + ");\n"
        + _FOOTER
    )
    return _write(tmp_path, body, "DuplicateParam")


@pytest.fixture
def missing_component_source(tmp_path: Path) -> Path:
    """A background layer whose component names a glyph that is not in the font.

    The background is where this occurs in practice and where it does damage:
    glyphsLib decomposes non-master layers by drawing them, and drawing a
    component that resolves to nothing raises.
    """
    background = """background = {
components = (
{
name = "_part.gone";
}
);
};
"""
    body = (
        _HEADER
        + _MASTER
        + "glyphs = (\n"
        + _square("A", "0041", extra=background)
        + ");\n"
        + _FOOTER
    )
    return _write(tmp_path, body, "MissingComponent")


@pytest.fixture
def duplicate_glyph_order_source(tmp_path: Path) -> Path:
    """A glyphOrder custom parameter that names the same glyph twice.

    Glyphs.app writes this list through unvalidated. The conversion itself
    succeeds -- what breaks is the UFO afterwards, because fontParts refuses to
    read a glyphOrder with a repeat in it.
    """
    body = (
        _HEADER
        + """customParameters = (
{
name = glyphOrder;
value = (
A,
B,
A
);
}
);
"""
        + _MASTER
        + "glyphs = (\n"
        + _square("A", "0041")
        + _square("B", "0042")
        + ");\n"
        + _FOOTER
    )
    return _write(tmp_path, body, "DuplicateOrder")


@pytest.fixture
def clean_source(tmp_path: Path) -> Path:
    body = _HEADER + _MASTER + "glyphs = (\n" + _square("A", "0041") + ");\n" + _FOOTER
    return _write(tmp_path, body, "Clean")


# ---------------------------------------------------------------------------
# Duplicate custom parameters
# ---------------------------------------------------------------------------


def test_duplicate_parameter_breaks_conversion_without_preflight(
    duplicate_parameter_source: Path, tmp_path: Path
):
    """Pin the failure the repair exists for, so it cannot silently stop happening."""
    font = glyphsLib.load(str(duplicate_parameter_source))

    with pytest.raises(RuntimeError, match="More than one value"):
        glyphsLib.to_designspace(font)


def test_preflight_drops_the_duplicate_and_keeps_the_first(duplicate_parameter_source: Path):
    font = glyphsLib.load(str(duplicate_parameter_source))

    warnings = preflight_source(font)

    names = [p.name for p in font.customParameters]
    assert names.count("fsType") == 1
    assert font.customParameters["fsType"] == [2]  # the first value, not the second
    assert len(warnings) == 1
    assert "fsType" in warnings[0].message
    assert warnings[0].category == "preflight"


def test_source_with_duplicate_parameter_now_converts(
    duplicate_parameter_source: Path, tmp_path: Path
):
    result = convert_glyphs_to_ufos(duplicate_parameter_source, tmp_path / "out")

    assert result.glyph_count == 1
    assert [w.message for w in result.warnings if w.category == "preflight"]


def test_preflight_is_stable_when_run_twice(duplicate_parameter_source: Path):
    font = glyphsLib.load(str(duplicate_parameter_source))

    first = preflight_source(font)
    second = preflight_source(font)

    assert first
    assert second == []


# ---------------------------------------------------------------------------
# glyphOrder listing a name twice
# ---------------------------------------------------------------------------


def test_duplicate_glyph_order_makes_the_ufo_unreadable_without_preflight(
    duplicate_glyph_order_source: Path, tmp_path: Path
):
    """Pin the failure the repair exists for.

    The conversion succeeds either way -- glyphsLib copies the list across
    verbatim -- so the damage only shows when something opens the result. The
    application drew an empty window here, because the exception came out of a
    getter in the grid's very first pass over the font.
    """
    import fontParts.fontshell as fp

    font = glyphsLib.load(str(duplicate_glyph_order_source))
    ufo = glyphsLib.to_ufos(font)[0]
    path = tmp_path / "raw.ufo"
    ufo.save(str(path), overwrite=True)

    with pytest.raises(ValueError, match="Duplicate glyph names"):
        fp.RFont(str(path), showInterface=False).glyphOrder


def test_preflight_drops_the_repeat_and_keeps_the_first_position(
    duplicate_glyph_order_source: Path,
):
    font = glyphsLib.load(str(duplicate_glyph_order_source))

    warnings = preflight_source(font)

    assert font.customParameters["glyphOrder"] == ["A", "B"]
    assert len(warnings) == 1
    assert "glyphOrder" in warnings[0].message
    assert "A" in (warnings[0].details or "")


def test_imported_ufo_has_a_readable_glyph_order(
    duplicate_glyph_order_source: Path, tmp_path: Path
):
    """The end the user sees: the imported font opens with its glyphs in it."""
    import fontParts.fontshell as fp

    result = convert_glyphs_to_ufos(duplicate_glyph_order_source, tmp_path / "out")

    font = fp.RFont(str(result.open_path), showInterface=False)
    assert list(font.glyphOrder) == ["A", "B"]
    assert len(font) == 2


def test_glyph_order_repair_is_stable_when_run_twice(duplicate_glyph_order_source: Path):
    font = glyphsLib.load(str(duplicate_glyph_order_source))

    first = preflight_source(font)
    second = preflight_source(font)

    assert first
    assert second == []


# ---------------------------------------------------------------------------
# Components pointing nowhere
# ---------------------------------------------------------------------------


def test_missing_component_breaks_conversion_without_preflight(missing_component_source: Path):
    from fontTools.pens.basePen import MissingComponentError

    font = glyphsLib.load(str(missing_component_source))

    with pytest.raises(MissingComponentError):
        doc = glyphsLib.to_designspace(font)
        doc.sources[0].font  # building the UFO is what draws the component


def test_preflight_removes_the_dangling_component(missing_component_source: Path):
    font = glyphsLib.load(str(missing_component_source))

    warnings = preflight_source(font)

    layer = font.glyphs["A"].layers[0]
    assert len(layer._background.components) == 0
    assert len(warnings) == 1
    assert "_part.gone" in warnings[0].message


def test_source_with_missing_component_now_converts(missing_component_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(missing_component_source, tmp_path / "out")

    assert result.glyph_count == 1


# ---------------------------------------------------------------------------
# The trap: backgrounds must not be created by looking for them
# ---------------------------------------------------------------------------


def test_preflight_does_not_materialise_background_layers(clean_source: Path):
    """`GSLayer.background` CREATES a background layer when there is none.

    An earlier version of the walk read it, gave every layer in the font an
    empty background, and turned a source that imported cleanly into
    `KeyError: "glyph named 'B' already exists"`. The walk must read
    `_background` instead.
    """
    font = glyphsLib.load(str(clean_source))
    assert all(layer._background is None for g in font.glyphs for layer in g.layers)

    preflight_source(font)

    assert all(layer._background is None for g in font.glyphs for layer in g.layers), (
        "preflight created background layers that the source does not have"
    )


def test_clean_source_needs_no_repairs(clean_source: Path):
    font = glyphsLib.load(str(clean_source))

    assert preflight_source(font) == []


# ---------------------------------------------------------------------------
# Counting, for the dialog's switches
# ---------------------------------------------------------------------------


def test_counts_are_zero_for_a_plain_source(clean_source: Path):
    font = glyphsLib.load(str(clean_source))

    assert count_component_hints(font) == 0
    assert count_hints_by_family(font) == (0, 0)


def test_hint_families_are_counted_separately(tmp_path: Path):
    hints = """hints = (
{
horizontal = 1;
place = "{100, 50}";
type = Stem;
},
{
origin = "{0, 1}";
type = TopGhost;
},
{
origin = "{0, 2}";
type = TTSnap;
}
);
"""
    body = _HEADER + _MASTER + "glyphs = (\n" + _square("A", "0041", extra=hints) + ");\n" + _FOOTER
    font = glyphsLib.load(str(_write(tmp_path, body, "Hinted")))

    ps, tt = count_hints_by_family(font)

    assert ps == 2  # Stem + TopGhost
    assert tt == 1  # TTSnap


# ---------------------------------------------------------------------------
# Two layers of one master, each with a background
# ---------------------------------------------------------------------------


@pytest.fixture
def colliding_background_source(tmp_path: Path) -> Path:
    """Two layers of one master with the same name, both with a background.

    glyphsLib names a background layer after the layer it belongs to, so two
    layers called the same thing claim the same `<name>.background` and the
    second one ends the import. Glyphs names a manual backup by the date it was
    taken, so two backups made in the same minute collide. a large production source has
    241 of them.
    """
    layer = """{
layerId = m01;
background = {
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"100 0 LINE",
"100 100 LINE"
);
}
);
};
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
},
{
associatedMasterId = m01;
layerId = "BACKUP-1";
name = "24 Jan 24 at 17:44";
background = {
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"50 0 LINE",
"50 50 LINE"
);
}
);
};
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"300 0 LINE",
"300 600 LINE"
);
}
);
width = 300;
},
{
associatedMasterId = m01;
layerId = "BACKUP-2";
name = "24 Jan 24 at 17:44";
background = {
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"60 0 LINE",
"60 60 LINE"
);
}
);
};
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"320 0 LINE",
"320 620 LINE"
);
}
);
width = 320;
}"""
    body = (
        _HEADER
        + _MASTER
        + "glyphs = (\n{\nglyphname = A;\nlayers = (\n"
        + layer
        + "\n);\nunicode = 0041;\n},\n);\n"
        + _FOOTER
    )
    return _write(tmp_path, body, "CollidingBackground")


def test_every_background_still_has_a_place_to_go(colliding_background_source: Path):
    """No two layers of a master may claim the same background layer name.

    That is what ends the import, and it is the property worth pinning: the
    fixture reproduces the shape, not glyphsLib's crash, which needs more of
    the real source than a fixture carries.
    """
    font = glyphsLib.load(str(colliding_background_source))

    preflight_source(font)

    for glyph in font.glyphs:
        seen: set[tuple[str, str]] = set()
        for layer in glyph.layers:
            if getattr(layer, "_background", None) is None:
                continue
            target = (
                "public.background"
                if layer.layerId == layer.associatedMasterId
                else f"{layer.name}.background"
            )
            key = (layer.associatedMasterId, target)
            assert key not in seen, f"{glyph.name}: two layers would write to {target}"
            seen.add(key)


def test_nothing_is_dropped_the_duplicate_name_is_changed(colliding_background_source: Path):
    """The repair renames, it does not delete.

    The first version of this dropped the backgrounds of every non-master
    layer -- 1193 of them in a large production source, where only 241 were in the way.
    A background is the designer's reference drawing, and glyphsLib itself
    renames duplicate *outline* layers with the same ` #1` suffix; doing the
    same for backgrounds keeps every drawing and matches what a round trip
    back to Glyphs would produce.
    """
    font = glyphsLib.load(str(colliding_background_source))
    before = sum(
        1 for g in font.glyphs for lay in g.layers if getattr(lay, "_background", None) is not None
    )

    warnings = preflight_source(font)

    after = sum(
        1 for g in font.glyphs for lay in g.layers if getattr(lay, "_background", None) is not None
    )
    assert after == before, "every background is kept"

    names = [lay.name for lay in font.glyphs["A"].layers if lay.layerId != "m01"]
    assert "24 Jan 24 at 17:44" in names
    assert "24 Jan 24 at 17:44 #1" in names, "the second one is renamed, as glyphsLib does"
    assert any("renamed a second layer" in w.message for w in warnings)


def test_source_with_two_backgrounds_now_converts(
    colliding_background_source: Path, tmp_path: Path
):
    result = convert_glyphs_to_ufos(colliding_background_source, tmp_path / "out")

    assert result.open_path.exists()


# ---------------------------------------------------------------------------
# A cubic segment with three off-curve points
# ---------------------------------------------------------------------------


@pytest.fixture
def degenerate_cubic_source(tmp_path: Path) -> Path:
    """Three off-curve points before a curve point, all on top of each other.

    A UFO cubic segment holds two, and the GLIF writer refuses a third. In
    a large production source the four contours shaped like this are zero-length: the
    off-curves are copies of the point they sit on, left behind by an edit.
    """
    layer = """{
layerId = m01;
paths = (
{
closed = 1;
nodes = (
"0 0 LINE",
"400 0 LINE",
"400 0 OFFCURVE",
"400 0 OFFCURVE",
"400 0 OFFCURVE",
"400 0 CURVE",
"400 700 LINE",
"0 700 LINE"
);
}
);
width = 400;
}"""
    body = (
        _HEADER
        + _MASTER
        + "glyphs = (\n{\nglyphname = A;\nlayers = (\n"
        + layer
        + "\n);\nunicode = 0041;\n},\n);\n"
        + _FOOTER
    )
    return _write(tmp_path, body, "DegenerateCubic")


def test_preflight_removes_only_the_duplicate_offcurves(degenerate_cubic_source: Path):
    font = glyphsLib.load(str(degenerate_cubic_source))
    before = len(font.glyphs["A"].layers[0].paths[0].nodes)

    warnings = preflight_source(font)

    nodes = font.glyphs["A"].layers[0].paths[0].nodes
    assert len(nodes) == before - 1, "one surplus off-curve removed, the rest untouched"
    offcurves = [n for n in nodes if n.type == "offcurve"]
    assert len(offcurves) == 2, "a cubic segment keeps its two off-curves"
    assert any("duplicate off-curve" in w.message for w in warnings)


def test_source_with_a_degenerate_cubic_now_converts(degenerate_cubic_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(degenerate_cubic_source, tmp_path / "out")

    assert result.open_path.exists()


# ---------------------------------------------------------------------------
# A cubic segment with distinct surplus off-curves
# ---------------------------------------------------------------------------

# Four different off-curves before a curve point: no UFO can hold it, and no
# point can be dropped without changing the shape.
_UNWRITABLE_PATH = """{
closed = 1;
nodes = (
"0 0 LINE",
"100 300 OFFCURVE",
"200 500 OFFCURVE",
"300 550 OFFCURVE",
"350 600 OFFCURVE",
"400 700 CURVE",
"0 700 LINE"
);
}"""

_PLAIN_PATH = """{
closed = 1;
nodes = (
"0 0 LINE",
"400 0 LINE",
"400 700 LINE",
"0 700 LINE"
);
}"""


def _unwritable_source(tmp_path: Path, layers: str) -> Path:
    body = (
        _HEADER
        + _MASTER
        + "glyphs = (\n{\nglyphname = A;\nlayers = (\n"
        + layers
        + "\n);\nunicode = 0041;\n},\n);\n"
        + _FOOTER
    )
    return _write(tmp_path, body, "Unwritable")


def test_unwritable_contour_in_a_background_is_removed(tmp_path: Path):
    source = _unwritable_source(
        tmp_path,
        "{\nbackground = {\npaths = (\n"
        + _UNWRITABLE_PATH
        + ",\n"
        + _PLAIN_PATH
        + "\n);\n};\nlayerId = m01;\npaths = (\n"
        + _PLAIN_PATH
        + "\n);\nwidth = 400;\n}",
    )

    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    assert any("removed a contour" in w.message for w in result.warnings)
    import ufoLib2

    font = ufoLib2.Font.open(result.open_path)
    assert len(font["A"].contours) == 1, "the drawn outline is untouched"
    assert len(font.layers["public.background"]["A"].contours) == 1, "only the bad contour went"


def test_unwritable_contour_in_a_backup_layer_is_removed(tmp_path: Path):
    source = _unwritable_source(
        tmp_path,
        "{\nlayerId = m01;\npaths = (\n"
        + _PLAIN_PATH
        + "\n);\nwidth = 400;\n},\n"
        + "{\nassociatedMasterId = m01;\nlayerId = BACKUP1;\nname = Backup;\npaths = (\n"
        + _UNWRITABLE_PATH
        + "\n);\nwidth = 400;\n}",
    )

    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    assert any("removed a contour" in w.message for w in result.warnings)


def test_unwritable_contour_in_a_master_stops_before_converting(tmp_path: Path):
    source = _unwritable_source(
        tmp_path,
        "{\nlayerId = m01;\npaths = (\n" + _UNWRITABLE_PATH + "\n);\nwidth = 400;\n}",
    )
    font = glyphsLib.load(str(source))

    with pytest.raises(UnwritableOutlineError, match="'A'"):
        preflight_source(font)


def test_duplicate_run_across_the_start_of_a_closed_path_is_repaired(tmp_path: Path):
    """Glyphs keeps the start point last, so a run can wrap from the end of the list."""
    path = """{
closed = 1;
nodes = (
"400 0 OFFCURVE",
"400 0 OFFCURVE",
"400 0 CURVE",
"400 700 LINE",
"0 700 LINE",
"0 0 LINE",
"400 0 OFFCURVE"
);
}"""
    source = _unwritable_source(
        tmp_path, "{\nlayerId = m01;\npaths = (\n" + path + "\n);\nwidth = 400;\n}"
    )
    font = glyphsLib.load(str(source))

    warnings = preflight_source(font)

    nodes = font.glyphs["A"].layers[0].paths[0].nodes
    assert sum(1 for n in nodes if n.type == "offcurve") == 2
    assert any("duplicate off-curve" in w.message for w in warnings)
    result = convert_glyphs_to_ufos(source, tmp_path / "out")
    assert result.open_path.exists()


# ---------------------------------------------------------------------------
# TrueType (quadratic) runs are legal at any length
# ---------------------------------------------------------------------------

# Four distinct off-curves before a qcurve point, and a second run that wraps
# across the start of the node list. Both are ordinary TrueType segments.
_QUADRATIC_PATH = """{
closed = 1;
nodes = (
"350 650 OFFCURVE",
"250 700 QCURVE",
"0 700 LINE",
"0 0 LINE",
"100 50 OFFCURVE",
"200 150 OFFCURVE",
"300 250 OFFCURVE",
"350 400 OFFCURVE",
"400 500 QCURVE",
"400 600 OFFCURVE"
);
}"""

# A closed quadratic contour with no on-curve point at all.
_ALL_OFFCURVE_PATH = """{
closed = 1;
nodes = (
"0 350 OFFCURVE",
"250 700 OFFCURVE",
"500 350 OFFCURVE",
"250 0 OFFCURVE"
);
}"""


@pytest.mark.parametrize("path", [_QUADRATIC_PATH, _ALL_OFFCURVE_PATH], ids=["runs", "no-oncurve"])
def test_quadratic_runs_are_left_alone(tmp_path: Path, path: str):
    source = _unwritable_source(
        tmp_path, "{\nlayerId = m01;\npaths = (\n" + path + "\n);\nwidth = 500;\n}"
    )
    font = glyphsLib.load(str(source))
    before = [
        (n.position.x, n.position.y, n.type) for n in font.glyphs["A"].layers[0].paths[0].nodes
    ]

    warnings = preflight_source(font)

    after = [
        (n.position.x, n.position.y, n.type) for n in font.glyphs["A"].layers[0].paths[0].nodes
    ]
    assert after == before
    assert warnings == []

    import ufoLib2

    result = convert_glyphs_to_ufos(source, tmp_path / "out")
    points = ufoLib2.Font.open(result.open_path)["A"].contours[0].points
    assert len(points) == len(before), "every point reaches the UFO"
