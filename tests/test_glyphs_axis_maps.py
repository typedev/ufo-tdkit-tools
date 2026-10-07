# Copyright 2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Tests for dropping an axis mapping that the masters contradict.

Headless. Without "Axis Location" parameters glyphsLib derives an axis's
user -> design map from the instances' weight/width classes; when those
contradict the instances' coordinates, the map can send every user value to one
design value, and nothing can interpolate the document.
"""

from pathlib import Path

import pytest

glyphsLib = pytest.importorskip("glyphsLib")

from fontTools.designspaceLib import (  # noqa: E402
    AxisDescriptor,
    DesignSpaceDocument,
    SourceDescriptor,
)

from ufo_tdkit_tools.glyphs import convert_glyphs_to_ufos  # noqa: E402
from ufo_tdkit_tools.glyphs.axis_maps import repair_axis_maps  # noqa: E402


def _doc(axis_map, minimum, default, maximum, masters=(100, 900)) -> DesignSpaceDocument:
    doc = DesignSpaceDocument()
    axis = AxisDescriptor()
    axis.name, axis.tag = "Weight", "wght"
    axis.minimum, axis.default, axis.maximum = minimum, default, maximum
    axis.map = list(axis_map)
    doc.addAxis(axis)
    for i, value in enumerate(masters):
        source = SourceDescriptor()
        source.filename = f"m{i}.ufo"
        source.location = {"Weight": value}
        source.copyLib = i == 0
        doc.addSource(source)
    return doc


def test_collapsed_mapping_is_dropped():
    doc = _doc([(100, 900), (400, 900), (800, 900)], 100, 100, 800)

    warnings = repair_axis_maps(doc)

    axis = doc.axes[0]
    assert axis.map == []
    assert (axis.minimum, axis.default, axis.maximum) == (100, 100, 900)
    assert len(warnings) == 1
    assert "wght" in warnings[0].message


def test_mapping_that_misses_the_masters_is_dropped():
    doc = _doc([(100, 300), (900, 600)], 100, 100, 900)

    assert repair_axis_maps(doc)
    assert doc.axes[0].map == []


def test_sound_mapping_is_kept():
    mapping = [(100, 100), (400, 350), (900, 900)]
    doc = _doc(mapping, 100, 100, 900)

    assert repair_axis_maps(doc) == []
    assert doc.axes[0].map == mapping
    assert (doc.axes[0].minimum, doc.axes[0].maximum) == (100, 900)


def test_mapping_whose_default_misses_the_default_master_is_dropped():
    doc = _doc([(100, 100), (400, 500), (900, 900)], 100, 400, 900)

    assert repair_axis_maps(doc)
    assert doc.axes[0].default == 100, "the default master's own coordinate"


_CONTRADICTORY_SOURCE = """{
.appVersion = "3260";
.formatVersion = 3;
axes = (
{
name = Weight;
tag = wght;
}
);
familyName = Contradictory;
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
glyphname = I;
layers = (
{
layerId = m01;
shapes = (
{
closed = 1;
nodes = (
(40,0,l),
(60,0,l),
(60,700,l),
(40,700,l)
);
}
);
width = 100;
},
{
layerId = m02;
shapes = (
{
closed = 1;
nodes = (
(40,0,l),
(250,0,l),
(250,700,l),
(40,700,l)
);
}
);
width = 290;
}
);
unicode = 73;
}
);
instances = (
{
axesValues = (
100
);
name = Thin;
weightClass = 100;
},
{
axesValues = (
900
);
name = Heavy;
weightClass = 100;
}
);
unitsPerEm = 1000;
versionMajor = 1;
versionMinor = 0;
}
"""


def test_import_of_contradictory_instances_gives_an_interpolatable_axis(tmp_path: Path):
    source = tmp_path / "Contradictory.glyphs"
    source.write_text(_CONTRADICTORY_SOURCE, encoding="utf-8")

    result = convert_glyphs_to_ufos(source, tmp_path / "out")

    axis = DesignSpaceDocument.fromfile(str(result.designspace_path)).axes[0]
    assert axis.map == []
    assert (axis.minimum, axis.default, axis.maximum) == (100, 100, 900)
    assert any("dropped a user -> design mapping" in w.message for w in result.warnings)
