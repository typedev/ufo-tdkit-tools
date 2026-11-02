# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Tests for compilation module (import checks, is_preserve_mode, width splitting)."""

from ufo_tdkit_tools.compilation import is_preserve_mode
from ufo_tdkit_tools.compilation.compiler import _outlines_match, _split_width_prefix


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


def _charstring(*contours):
    """T2 charstring from contours given as lists of pen calls.

    Each call is ``("line", pt)`` or ``("curve", c1, c2, pt)``; the first
    element of a contour is its start point.
    """
    from fontTools.cffLib import PrivateDict
    from fontTools.pens.t2CharStringPen import T2CharStringPen

    pen = T2CharStringPen(500, None, roundTolerance=0)
    for start, *calls in contours:
        pen.moveTo(start)
        for kind, *pts in calls:
            if kind == "line":
                pen.lineTo(pts[0])
            else:
                pen.curveTo(*pts)
        pen.closePath()
    return pen.getCharString(private=PrivateDict())


SQUARE = [(0, 0), ("line", (100, 0)), ("line", (100, 100)), ("line", (0, 100))]
INNER = [(20, 20), ("line", (20, 80)), ("line", (80, 80)), ("line", (80, 20))]


class TestOutlinesMatch:
    """Donor/shell outline comparison: tolerant of encoding, strict on shape."""

    def test_identical(self):
        assert _outlines_match(_charstring(SQUARE, INNER), _charstring(SQUARE, INNER))

    def test_explicit_closing_line(self):
        closed = SQUARE + [("line", (0, 0))]
        assert _outlines_match(_charstring(SQUARE), _charstring(closed))

    def test_fractional_coordinates_within_one_unit(self):
        frac = [(0.4, 0), ("line", (100.6, 0)), ("line", (100, 100.5)), ("line", (0, 99.5))]
        assert _outlines_match(_charstring(SQUARE), _charstring(frac))

    def test_collinear_interior_points(self):
        split = [(0, 0), ("line", (40, 0)), ("line", (100, 0)), ("line", (100, 100))]
        split += [("line", (0, 100)), ("line", (0, 50))]
        assert _outlines_match(_charstring(SQUARE), _charstring(split))

    def test_retracted_curve_is_a_line(self):
        curved = [(0, 0), ("curve", (0, 0), (100, 0), (100, 0))] + SQUARE[2:]
        assert _outlines_match(_charstring(SQUARE), _charstring(curved))

    def test_tiny_segment_from_rounding(self):
        nick = [(0, 0), ("line", (99, 0)), ("line", (100, 0))] + SQUARE[2:]
        assert _outlines_match(_charstring(SQUARE), _charstring(nick))

    def test_other_start_point_and_direction(self):
        """ufo2ft reverses mirrored components, tx does not."""
        reversed_square = [(100, 100), ("line", (100, 0)), ("line", (0, 0)), ("line", (0, 100))]
        assert _outlines_match(_charstring(SQUARE), _charstring(reversed_square))

    def test_contour_order(self):
        assert _outlines_match(_charstring(SQUARE, INNER), _charstring(INNER, SQUARE))

    def test_merged_contours_differ(self):
        """The tx bug: the 2nd contour's moveto is dropped and becomes a lineto."""
        merged = SQUARE + [("line", (0, 0))] + [("line", INNER[0])] + INNER[1:]
        assert not _outlines_match(_charstring(SQUARE, INNER), _charstring(merged))

    def test_missing_contour_differs(self):
        assert not _outlines_match(_charstring(SQUARE, INNER), _charstring(SQUARE))

    def test_moved_point_differs(self):
        moved = SQUARE[:2] + [("line", (100, 105))] + SQUARE[3:]
        assert not _outlines_match(_charstring(SQUARE), _charstring(moved))

    def test_curve_vs_line_differs(self):
        bulge = [(0, 0), ("curve", (30, -20), (70, -20), (100, 0))] + SQUARE[2:]
        assert not _outlines_match(_charstring(SQUARE), _charstring(bulge))

    def test_point_two_units_off_a_straight_run(self):
        """Each compile may round the point one step off the line, in opposite ways."""
        kinked = SQUARE + [("line", (2, 50))]
        assert _outlines_match(_charstring(SQUARE), _charstring(kinked))

    def test_spur_back_along_a_straight_run(self):
        spur = [(0, 0), ("line", (103, 0)), ("line", (100, 0))] + SQUARE[2:]
        assert _outlines_match(_charstring(SQUARE), _charstring(spur))

    def test_corner_three_units_off_differs(self):
        kinked = SQUARE + [("line", (3, 50))]
        assert not _outlines_match(_charstring(SQUARE), _charstring(kinked))
