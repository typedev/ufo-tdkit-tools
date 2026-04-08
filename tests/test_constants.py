# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for shared constants and compute_outline_hash."""

from unittest.mock import MagicMock

from ufo_tdkit_tools.constants import (
    ADOBE_HINT_KEY_V2,
    ADOBE_HINT_KEY_V1,
    MAX_STEMS_PER_HINTSET,
    PROCESSED_LAYER_NAME,
    PUBLIC_PS_HINT_KEY,
    VALID_STEM_TYPES,
    _norm_float,
    compute_outline_hash,
)


class TestConstants:
    def test_adobe_hint_key_v2(self):
        assert ADOBE_HINT_KEY_V2 == "com.adobe.type.autohint.v2"

    def test_adobe_hint_key_v1(self):
        assert ADOBE_HINT_KEY_V1 == "com.adobe.type.autohint"

    def test_public_ps_hint_key(self):
        assert PUBLIC_PS_HINT_KEY == "public.postscript.hints"

    def test_processed_layer_name(self):
        assert PROCESSED_LAYER_NAME == "com.adobe.type.processedglyphs"

    def test_valid_stem_types(self):
        assert VALID_STEM_TYPES == {"hstem", "vstem", "hstem3", "vstem3"}

    def test_max_stems(self):
        assert MAX_STEMS_PER_HINTSET == 96


class TestNormFloat:
    def test_integer_value(self):
        assert _norm_float(100.0) == "100"

    def test_float_value(self):
        assert _norm_float(50.5) == "50.5"

    def test_zero(self):
        assert _norm_float(0.0) == "0"

    def test_negative(self):
        assert _norm_float(-20.0) == "-20"


class TestComputeOutlineHash:
    def _make_mock_glyph(self, width=500, points=None, components=None):
        glyph = MagicMock()
        glyph.width = width
        glyph.components = components or []

        if points:
            mock_contour = MagicMock()
            mock_points = []
            for x, y, ptype in points:
                p = MagicMock()
                p.x = x
                p.y = y
                p.type = ptype
                mock_points.append(p)
            mock_contour.points = mock_points
            glyph.__iter__ = MagicMock(return_value=iter([mock_contour]))
        else:
            glyph.__iter__ = MagicMock(return_value=iter([]))

        return glyph

    def test_empty_glyph(self):
        glyph = self._make_mock_glyph(width=0)
        h = compute_outline_hash(glyph)
        assert h == "w0"

    def test_simple_glyph(self):
        glyph = self._make_mock_glyph(
            width=500,
            points=[
                (0, 0, "line"),
                (500, 0, "line"),
                (500, 700, "line"),
                (0, 700, "line"),
            ],
        )
        h = compute_outline_hash(glyph)
        assert h.startswith("w500")
        assert "l00" in h  # line point at (0,0)

    def test_deterministic(self):
        h1 = compute_outline_hash(self._make_mock_glyph(
            width=600,
            points=[(10, 20, "curve"), (30, 40, "offcurve")],
        ))
        h2 = compute_outline_hash(self._make_mock_glyph(
            width=600,
            points=[(10, 20, "curve"), (30, 40, "offcurve")],
        ))
        assert h1 == h2

    def test_none_width(self):
        glyph = self._make_mock_glyph(width=None)
        glyph.width = None
        h = compute_outline_hash(glyph)
        assert h == "w0"

    def test_long_hash_uses_sha512(self):
        # Create a glyph with many points so the data exceeds 128 chars
        points = [(float(i), float(i * 2), "line") for i in range(50)]
        glyph = self._make_mock_glyph(width=500, points=points)
        h = compute_outline_hash(glyph)
        # SHA-512 hex digest is 128 chars
        assert len(h) == 128
