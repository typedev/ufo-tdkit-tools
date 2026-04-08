# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for compilation module (import checks and is_preserve_mode)."""

from ufo_tdkit_tools.compilation import is_preserve_mode


class TestIsPreserveMode:
    def test_preserve(self):
        assert is_preserve_mode("preserve") is True

    def test_preserve_optimized(self):
        assert is_preserve_mode("preserve-optimized") is True

    def test_true_bool(self):
        assert is_preserve_mode(True) is False

    def test_false_bool(self):
        assert is_preserve_mode(False) is False

    def test_none(self):
        assert is_preserve_mode(None) is False

    def test_empty_string(self):
        assert is_preserve_mode("") is False

    def test_other_string(self):
        assert is_preserve_mode("autohint") is False

    def test_int(self):
        assert is_preserve_mode(1) is False
