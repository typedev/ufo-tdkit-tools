# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Public API contract.

TDKit (and any other downstream) imports these names from these module paths
and passes these keyword arguments. Renaming or removing any of them is a
breaking change and needs a minor-version bump plus a CHANGELOG entry --
this file exists so that such a change cannot happen by accident.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

import ufo_tdkit_tools


def _params(func):
    return set(inspect.signature(func).parameters)


def _fields(cls):
    return {f.name for f in dataclasses.fields(cls)}


class TestTopLevel:
    def test_version_is_package_metadata(self):
        from importlib.metadata import version

        assert ufo_tdkit_tools.__version__ == version("ufo-tdkit-tools")

    def test_lazy_names_resolve(self):
        pytest.importorskip("ufo2ft")
        assert callable(ufo_tdkit_tools.process_font)
        assert dataclasses.is_dataclass(ufo_tdkit_tools.ProcessResult)
        assert callable(ufo_tdkit_tools.add_legacy_kern)


class TestPipelineApi:
    def test_process_font_signature(self):
        from ufo_tdkit_tools.pipeline import process_font

        assert {
            "hint_source",
            "autohint",
            "optimize",
            "tx_path",
            "makeotf_path",
            "logger_",
        } <= _params(process_font)

    def test_process_result_fields(self):
        from ufo_tdkit_tools.pipeline import ProcessResult

        assert {"success", "error"} <= _fields(ProcessResult)


class TestCompilationApi:
    @pytest.fixture(autouse=True)
    def _needs_compilation_extra(self):
        pytest.importorskip("ufo2ft")

    def test_compile_functions_signature(self):
        from ufo_tdkit_tools.compilation import (
            compile_otf_preserve,
            compile_otf_preserve_optimized,
        )

        for func in (compile_otf_preserve, compile_otf_preserve_optimized):
            assert {"logger", "tx_path", "makeotf_path"} <= _params(func)

    def test_is_preserve_mode_exported(self):
        from ufo_tdkit_tools.compilation import is_preserve_mode

        assert callable(is_preserve_mode)

    def test_preserve_compile_signature(self):
        from ufo_tdkit_tools.compilation import preserve_compile, preserve_compile_batch

        assert {"logger", "tx_path", "makeotf_path"} <= _params(preserve_compile)
        assert {"logger", "parallel", "workers", "on_progress"} <= _params(preserve_compile_batch)

    def test_preserve_result_fields(self):
        from ufo_tdkit_tools.compilation import BatchCompileResult, PreserveCompileResult

        assert {
            "success",
            "skipped",
            "error",
            "ufo_path",
            "otf_path",
            "hints_found",
            "hints_transferred",
        } <= _fields(PreserveCompileResult)

        batch = BatchCompileResult()
        assert isinstance(batch.to_dict(), dict)
        for attr in ("successful", "failed", "skipped_count", "total"):
            assert hasattr(batch, attr), attr

    def test_removed_helpers_stay_removed(self):
        from ufo_tdkit_tools import compilation

        for name in ("generate_goadb", "prepare_processedglyphs"):
            assert name not in compilation.__all__
            with pytest.raises(AttributeError):
                getattr(compilation, name)


class TestPsHintsApi:
    def test_validate_ps_hints_result_keys(self, tmp_path):
        defcon = pytest.importorskip("defcon")
        from ufo_tdkit_tools.ps_hints import validate_ps_hints

        ufo_path = tmp_path / "empty.ufo"
        defcon.Font().save(str(ufo_path))

        report = validate_ps_hints(str(ufo_path))
        assert {"valid", "glyphs_checked", "glyphs_with_hints", "errors", "warnings"} <= set(report)
