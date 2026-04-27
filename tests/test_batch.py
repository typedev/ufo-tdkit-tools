# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for ps_hints.batch whole-font wrappers."""

from __future__ import annotations

from ufo_tdkit_tools.constants import (
    ADOBE_HINT_KEY_V2,
    PROCESSED_LAYER_NAME,
    PUBLIC_PS_HINT_KEY,
)
from ufo_tdkit_tools.ps_hints.batch import (
    _to_source_string,
    count_glyphs_with_source,
    detect_font_source,
)
from ufo_tdkit_tools.ps_hints.parser import HintSource


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeGlyph:
    def __init__(self, name, lib=None):
        self.name = name
        self.lib = lib or {}


class _FakeLayer:
    def __init__(self, name, glyphs=None):
        self.name = name
        self._glyphs = {g.name: g for g in (glyphs or [])}

    def __contains__(self, name):
        return name in self._glyphs

    def __getitem__(self, name):
        return self._glyphs[name]


class _FakeFont:
    def __init__(self, glyphs=None, extra_layers=None):
        self._glyphs = list(glyphs or [])
        default = _FakeLayer("public.default", self._glyphs)
        self.layers = [default] + list(extra_layers or [])

    def __iter__(self):
        return iter(self._glyphs)

    def __len__(self):
        return len(self._glyphs)

    def getLayer(self, name):
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(name)


def _hint_dict():
    return {"hintSetList": [{"stems": ["hstem 0 100"]}]}


# ── _to_source_string ─────────────────────────────────────────────────────────


class TestToSourceString:
    def test_string_passthrough(self):
        assert _to_source_string("v2") == "v2"
        assert _to_source_string("public_ps") == "public_ps"
        assert _to_source_string("processed") == "processed"

    def test_enum_to_string(self):
        assert _to_source_string(HintSource.AUTOHINT_V2) == "v2"
        assert _to_source_string(HintSource.PUBLIC_PS) == "public_ps"
        assert _to_source_string(HintSource.PROCESSED_LAYER) == "processed"


# ── detect_font_source ────────────────────────────────────────────────────────


class TestDetectFontSource:
    def test_no_hints_returns_none(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a"), _FakeGlyph("b")])
        assert detect_font_source(font) is None

    def test_v2_only(self):
        font = _FakeFont(
            glyphs=[
                _FakeGlyph("a", {ADOBE_HINT_KEY_V2: _hint_dict()}),
                _FakeGlyph("b"),
            ]
        )
        assert detect_font_source(font) == HintSource.AUTOHINT_V2

    def test_public_ps_only(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a", {PUBLIC_PS_HINT_KEY: _hint_dict()})])
        assert detect_font_source(font) == HintSource.PUBLIC_PS

    def test_processed_wins_over_v2(self):
        a = _FakeGlyph("a", {ADOBE_HINT_KEY_V2: _hint_dict()})
        pg_a = _FakeGlyph("a", {ADOBE_HINT_KEY_V2: _hint_dict()})
        font = _FakeFont(
            glyphs=[a],
            extra_layers=[_FakeLayer(PROCESSED_LAYER_NAME, [pg_a])],
        )
        assert detect_font_source(font) == HintSource.PROCESSED_LAYER

    def test_v2_wins_over_public_ps(self):
        a = _FakeGlyph(
            "a",
            {ADOBE_HINT_KEY_V2: _hint_dict(), PUBLIC_PS_HINT_KEY: _hint_dict()},
        )
        font = _FakeFont(glyphs=[a])
        assert detect_font_source(font) == HintSource.AUTOHINT_V2

    def test_mixed_glyphs_priority(self):
        # one glyph has v2, another has only public_ps -- whole-font picks v2
        a = _FakeGlyph("a", {ADOBE_HINT_KEY_V2: _hint_dict()})
        b = _FakeGlyph("b", {PUBLIC_PS_HINT_KEY: _hint_dict()})
        font = _FakeFont(glyphs=[a, b])
        assert detect_font_source(font) == HintSource.AUTOHINT_V2


# ── count_glyphs_with_source ──────────────────────────────────────────────────


class TestCountGlyphsWithSource:
    def test_count_v2(self):
        font = _FakeFont(
            glyphs=[
                _FakeGlyph("a", {ADOBE_HINT_KEY_V2: _hint_dict()}),
                _FakeGlyph("b"),
                _FakeGlyph("c", {ADOBE_HINT_KEY_V2: _hint_dict()}),
            ]
        )
        assert count_glyphs_with_source(font, HintSource.AUTOHINT_V2) == 2

    def test_count_public_ps_zero(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a", {ADOBE_HINT_KEY_V2: _hint_dict()})])
        assert count_glyphs_with_source(font, HintSource.PUBLIC_PS) == 0
