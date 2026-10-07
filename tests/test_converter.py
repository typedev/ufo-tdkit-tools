# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for ps_hints.converter point-tag handling between layers."""

from __future__ import annotations

import pytest

from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2, PROCESSED_LAYER_NAME
from ufo_tdkit_tools.ps_hints.converter import export_from_processed, import_to_processed

fp = pytest.importorskip("fontParts.world")

STEMS = ["hstem 614 126", "vstem 63 74", "hstem 21 -21", "hstem 547 -20"]


def _draw_i(glyph, dot_first: bool) -> None:
    """Draw an ``i``: a stem contour and a tittle contour."""
    stem = [(63, 547), (63, 0), (137, 0), (137, 547)]
    dot = [(63, 740), (63, 614), (137, 614), (137, 740)]
    pen = glyph.getPen()
    for pts in (dot, stem) if dot_first else (stem, dot):
        pen.moveTo(pts[0])
        for pt in pts[1:]:
            pen.lineTo(pt)
        pen.closePath()
    glyph.width = 200


def _point_at(glyph, name):
    for contour in glyph.contours:
        for point in contour.points:
            if point.name == name:
                return (point.x, point.y)
    return None


class TestExportPointTags:
    def _font(self):
        font = fp.NewFont()
        glyph = font.newGlyph("i")
        _draw_i(glyph, dot_first=True)
        # Stale name from an earlier hinting run, now on the second contour.
        glyph.contours[1].points[0].name = "hintRef0000"

        processed = font.newLayer(PROCESSED_LAYER_NAME).newGlyph("i")
        _draw_i(processed, dot_first=True)
        # The autohinter names the first point of the processed glyph.
        processed.contours[0].points[0].name = "hintRef0000"
        processed.lib[ADOBE_HINT_KEY_V2] = {
            "formatVersion": "1",
            "hintSetList": [{"pointTag": "hintRef0000", "stems": list(STEMS)}],
        }
        return font, glyph

    def test_stale_default_name_does_not_capture_tag(self):
        font, glyph = self._font()
        assert export_from_processed(glyph, font, "v2")
        tag = glyph.lib[ADOBE_HINT_KEY_V2]["hintSetList"][0]["pointTag"]
        assert _point_at(glyph, tag) == (63, 740)

    def test_existing_name_on_target_point_is_reused(self):
        font, glyph = self._font()
        glyph.contours[0].points[0].name = "authored"
        assert export_from_processed(glyph, font, "v2")
        assert glyph.lib[ADOBE_HINT_KEY_V2]["hintSetList"][0]["pointTag"] == "authored"

    def test_flex_names_follow_their_points(self):
        font, glyph = self._font()
        processed = font.getLayer(PROCESSED_LAYER_NAME)["i"]
        processed.contours[1].points[2].name = "hintRef0001"
        processed.lib[ADOBE_HINT_KEY_V2]["flexList"] = ["hintRef0001"]
        assert export_from_processed(glyph, font, "v2")
        flex = glyph.lib[ADOBE_HINT_KEY_V2]["flexList"]
        assert _point_at(glyph, flex[0]) == (137, 0)


class TestImportPointTags:
    def test_substitution_points_keep_their_positions(self):
        font = fp.NewFont()
        glyph = font.newGlyph("i")
        _draw_i(glyph, dot_first=False)
        glyph.contours[0].points[0].name = "start"
        glyph.contours[1].points[0].name = "dot"
        glyph.lib[ADOBE_HINT_KEY_V2] = {
            "formatVersion": "1",
            "hintSetList": [
                {"pointTag": "start", "stems": ["vstem 63 74"]},
                {"pointTag": "dot", "stems": ["hstem 614 126", "vstem 63 74"]},
            ],
        }
        assert import_to_processed(glyph, font, "v2")
        processed = font.getLayer(PROCESSED_LAYER_NAME)["i"]
        tags = [hs["pointTag"] for hs in processed.lib[ADOBE_HINT_KEY_V2]["hintSetList"]]
        assert _point_at(processed, tags[0]) == (63, 547)
        assert _point_at(processed, tags[1]) == (63, 740)
