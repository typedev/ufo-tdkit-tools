# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for pipeline.process_font dispatcher and helpers."""

from __future__ import annotations

import logging
from pathlib import Path

from ufo_tdkit_tools.pipeline import ProcessResult, _resolve_source, process_font
from ufo_tdkit_tools.ps_hints.parser import HintSource


# ── Fakes (shared shape with test_batch) ─────────────────────────────────────


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
        self.layers = [_FakeLayer("public.default", self._glyphs)] + list(extra_layers or [])

    def __iter__(self):
        return iter(self._glyphs)

    def __len__(self):
        return len(self._glyphs)

    def getLayer(self, name):
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(name)


# ── ProcessResult ─────────────────────────────────────────────────────────────


class TestProcessResult:
    def test_defaults(self):
        r = ProcessResult(success=False)
        assert r.success is False
        assert r.hint_source_used is None
        assert r.glyphs_total == 0
        assert r.optimized is False
        assert r.autohinted is False
        assert r.error is None

    def test_populated(self):
        r = ProcessResult(
            success=True,
            hint_source_used="autohint_v2",
            glyphs_total=200,
            glyphs_with_hints=180,
            optimized=True,
            optimized_count=180,
        )
        assert r.success is True
        assert r.glyphs_with_hints == 180
        assert r.optimized_count == 180


# ── _resolve_source ───────────────────────────────────────────────────────────


class TestResolveSource:
    def setup_method(self):
        self.log = logging.getLogger("test")

    def test_binary_always_v2(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a")])
        assert _resolve_source(font, "auto", True, self.log) == HintSource.AUTOHINT_V2
        # Even explicit hint_source is ignored for binary
        assert _resolve_source(font, "public_ps", True, self.log) == HintSource.AUTOHINT_V2

    def test_auto_with_no_hints_returns_none(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a")])
        assert _resolve_source(font, "auto", False, self.log) is None

    def test_explicit_v2_with_hints(self):
        from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2

        font = _FakeFont(
            glyphs=[_FakeGlyph("a", {ADOBE_HINT_KEY_V2: {"hintSetList": [{"stems": []}]}})]
        )
        assert _resolve_source(font, "v2", False, self.log) == HintSource.AUTOHINT_V2

    def test_explicit_v2_without_hints_returns_none(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a")])
        assert _resolve_source(font, "v2", False, self.log) is None

    def test_unknown_source_returns_none(self):
        font = _FakeFont(glyphs=[_FakeGlyph("a")])
        assert _resolve_source(font, "garbage", False, self.log) is None


# ── process_font dispatch (early exits, no compile) ──────────────────────────


class TestProcessFontDispatch:
    def test_missing_input(self, tmp_path):
        result = process_font(
            tmp_path / "nope.otf",
            tmp_path / "out.otf",
            tmp_path / "out.ufo",
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_unsupported_format(self, tmp_path):
        bogus = tmp_path / "x.txt"
        bogus.write_text("nope")
        result = process_font(
            bogus,
            tmp_path / "out.otf",
            tmp_path / "out.ufo",
        )
        assert result.success is False
        assert "unsupported" in result.error.lower()

    def test_forwards_tool_paths_to_compiler(self, tmp_path, monkeypatch):
        """tx_path/makeotf_path reach compile_otf_preserve_optimized so that
        ProcessPoolExecutor workers (no AFDKO on PATH) can compile."""
        import ufo_tdkit_tools.compilation.compiler as compiler
        import ufo_tdkit_tools.pipeline as pipeline
        import ufo_tdkit_tools.ps_hints.batch as batch

        ufo_in = tmp_path / "in.ufo"
        ufo_in.mkdir()

        fake = _FakeFont(glyphs=[_FakeGlyph("A")])
        fake.save = lambda path: Path(path).mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(pipeline, "_load_input", lambda *a, **k: (fake, None))
        monkeypatch.setattr(batch, "detect_font_source", lambda font: HintSource.AUTOHINT_V2)
        monkeypatch.setattr(batch, "count_glyphs_with_source", lambda font, src: 1)

        captured = {}

        def _fake_compile(ufo, otf, logger=None, tx_path=None, makeotf_path=None, **kw):
            captured["tx_path"] = tx_path
            captured["makeotf_path"] = makeotf_path
            Path(otf).write_bytes(b"OTF")
            return True

        monkeypatch.setattr(compiler, "compile_otf_preserve_optimized", _fake_compile)

        result = process_font(
            ufo_in,
            tmp_path / "out.otf",
            tmp_path / "out.ufo",
            hint_source="auto",
            optimize=False,
            tx_path="/opt/afdko/tx",
            makeotf_path="/opt/afdko/makeotf",
        )

        assert result.success is True
        assert captured == {"tx_path": "/opt/afdko/tx", "makeotf_path": "/opt/afdko/makeotf"}
