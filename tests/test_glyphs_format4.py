# Copyright 2026 TypeDev
# Licensed under the Apache License, Version 2.0

"""
Tests for reading a Glyphs 4 (`.formatVersion = 4`) source.

Headless — glyphsLib only, no GTK. glyphsLib reads formats 2 and 3; a format-4
file went to its format-2 readers and failed on the first node. The fixture
carries the three format-4 differences seen in a real source: no top-level
`familyName`, no instance `name`, and a `ct` node type.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")

from ufo_tdkit_tools.glyphs import (  # noqa: E402
    convert_glyphs_to_ufos,
    format_source_summary,
    inspect_glyphs_source,
)
from ufo_tdkit_tools.glyphs.fast_inspect import quick_inspect  # noqa: E402
from ufo_tdkit_tools.glyphs.format4 import load_glyphs_source  # noqa: E402

_FORMAT4_SOURCE = """{
.appVersion = "4107";
.formatVersion = 4;
axes = (
{
name = Weight;
tag = wght;
}
);
fontMaster = (
{
axesValues = (
100
);
id = m01;
name = Light;
},
{
axesValues = (
900
);
id = m02;
name = Black;
}
);
glyphs = (
{
glyphname = O;
layers = (
{
layerId = m01;
shapes = (
{
closed = 1;
nodes = (
(0,350,ct),
(0,550,o),
(150,700,o),
(250,700,cs),
(350,700,o),
(500,550,o),
(500,350,c),
(500,150,o),
(350,0,o),
(250,0,cs),
(150,0,o),
(0,150,o)
);
}
);
width = 500;
},
{
layerId = m02;
shapes = (
{
closed = 1;
nodes = (
(0,350,ct),
(0,550,o),
(150,700,o),
(250,700,cs),
(350,700,o),
(500,550,o),
(500,350,c),
(500,150,o),
(350,0,o),
(250,0,cs),
(150,0,o),
(0,150,o)
);
}
);
width = 600;
}
);
unicode = 79;
}
);
instances = (
{
axesValues = (
400
);
id = "F7160BBF-6731-4831-B47C-371CCCC3121B";
properties = (
{
key = styleNames;
values = (
{
language = dflt;
value = Book;
}
);
}
);
}
);
properties = (
{
key = familyNames;
values = (
{
language = dflt;
value = FormatFour;
}
);
}
);
unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
}
"""


@pytest.fixture
def format4_source(tmp_path: Path) -> Path:
    path = tmp_path / "FormatFour.glyphs"
    path.write_text(_FORMAT4_SOURCE, encoding="utf-8")
    return path


def test_plain_glyphslib_cannot_read_format_4(format4_source: Path):
    """Pins the reason this module exists; drop it once glyphsLib reads format 4."""
    with pytest.raises(TypeError):
        glyphsLib.load(str(format4_source))


def test_format_4_is_read_as_format_3(format4_source: Path):
    font = load_glyphs_source(format4_source)

    assert font.format_version == 3
    assert font.familyName == "FormatFour"
    assert [i.name for i in font.instances] == ["Book"]


def test_tangent_node_is_read_as_a_smooth_curve(format4_source: Path):
    font = load_glyphs_source(format4_source)

    first = font.glyphs["O"].layers[0].paths[0].nodes[0]
    assert first.type == "curve"
    assert first.smooth


def test_both_inspections_report_the_format(format4_source: Path):
    full = inspect_glyphs_source(format4_source)
    quick = quick_inspect(format4_source)

    for info in (full, quick):
        assert info.format_version == 4
        assert info.family_name == "FormatFour"
        assert "Glyphs 4 format" in format_source_summary(info)


def test_format_4_source_converts_and_says_so(format4_source: Path, tmp_path: Path):
    result = convert_glyphs_to_ufos(format4_source, tmp_path / "out")

    assert result.is_designspace
    assert sorted(p.name for p in result.ufo_paths) == [
        "FormatFour-Black.ufo",
        "FormatFour-Light.ufo",
    ]
    assert result.warnings[0].message == "Glyphs 4 format source: import is experimental"


def test_format_3_source_gets_no_format_note(tmp_path: Path):
    source = tmp_path / "Three.glyphs"
    source.write_text(
        _FORMAT4_SOURCE.replace(".formatVersion = 4;", ".formatVersion = 3;\nfamilyName = Three;")
        .replace(",ct)", ",c)")
        .replace('id = "F7160BBF', 'name = Book;\nid = "F7160BBF'),
        encoding="utf-8",
    )

    info = inspect_glyphs_source(source)
    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    assert info.format_version == 3
    assert not any("Glyphs 4" in w.message for w in result.warnings)
