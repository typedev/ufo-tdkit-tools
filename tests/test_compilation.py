# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for compilation module (import checks, is_preserve_mode, width splitting)."""

from ufo_tdkit_tools.compilation import is_preserve_mode
from ufo_tdkit_tools.compilation.compiler import _split_width_prefix


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


class TestSplitWidthPrefix:
    """The charstring merge must not carry the donor's width operand over.

    The optional leading width is encoded relative to the source font's
    ``Private.nominalWidthX``; detecting it relies on the argument count of the
    first stack-clearing operator (Type 2 spec).
    """

    def test_stem_op_even_args_has_no_width(self):
        prog = [0, 700, "hstem", 100, 400, "vstem", 100, 0, "rmoveto", "endchar"]
        assert _split_width_prefix(prog) == ([], prog)

    def test_stem_op_odd_args_has_width(self):
        prog = [600, 0, 700, "hstem", 100, 0, "rmoveto", "endchar"]
        assert _split_width_prefix(prog) == ([600], prog[1:])

    def test_hintmask_odd_args_has_width(self):
        prog = [600, 0, 700, "hintmask", "\xc0", 100, 0, "rmoveto", "endchar"]
        assert _split_width_prefix(prog) == ([600], prog[1:])

    def test_rmoveto_two_args_has_no_width(self):
        prog = [100, 0, "rmoveto", "endchar"]
        assert _split_width_prefix(prog) == ([], prog)

    def test_rmoveto_three_args_has_width(self):
        prog = [600, 100, 0, "rmoveto", "endchar"]
        assert _split_width_prefix(prog) == ([600], prog[1:])

    def test_hmoveto_one_arg_has_no_width(self):
        prog = [100, "hmoveto", 400, "hlineto", "endchar"]
        assert _split_width_prefix(prog) == ([], prog)

    def test_hmoveto_two_args_has_width(self):
        prog = [600, 100, "hmoveto", 400, "hlineto", "endchar"]
        assert _split_width_prefix(prog) == ([600], prog[1:])

    def test_vmoveto_two_args_has_width(self):
        prog = [600, 700, "vmoveto", "endchar"]
        assert _split_width_prefix(prog) == ([600], prog[1:])

    def test_bare_endchar_has_no_width(self):
        prog = ["endchar"]
        assert _split_width_prefix(prog) == ([], prog)

    def test_endchar_one_arg_has_width(self):
        prog = [500, "endchar"]
        assert _split_width_prefix(prog) == ([500], ["endchar"])

    def test_endchar_four_args_is_seac_without_width(self):
        prog = [0, 0, 65, 205, "endchar"]
        assert _split_width_prefix(prog) == ([], prog)

    def test_endchar_five_args_is_seac_with_width(self):
        prog = [600, 0, 0, 65, 205, "endchar"]
        assert _split_width_prefix(prog) == ([600], prog[1:])

    def test_empty_program(self):
        assert _split_width_prefix([]) == ([], [])

    def test_unknown_leading_operator_left_alone(self):
        prog = [107, "callsubr", "endchar"]
        assert _split_width_prefix(prog) == ([], prog)

    def test_result_concatenates_to_original(self):
        prog = [600, 0, 700, "hstem", 100, 0, "rmoveto", "endchar"]
        width, body = _split_width_prefix(prog)
        assert width + body == prog
