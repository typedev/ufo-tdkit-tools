# Copyright 2026 TypeDev
# Licensed under the Apache License, Version 2.0

"""
Cleaning the stem snap lists glyphsLib copies from a Glyphs source.

Glyphs 3 keeps the stem *definitions* on the font and one value per definition
on every master; a master with no value of its own holds 0. Merging UFOs into
one Glyphs file makes exactly that: each UFO adds its stems as new definitions,
and every other master gets 0 in those slots. glyphsLib writes the master's
values into `postscriptStemSnapH` / `postscriptStemSnapV` as they are, so the
UFO receives lists like `[0, 0, 67, 41, 0, 0, 0, 0, 0, 0, 0, 0]`.

A zero stem means nothing to a rasterizer, and a leading one is worse: ufo2ft
takes `StdHW` / `StdVW` from index 0 of the list as given (it sorts only the
`StemSnap*` arrays), so `[0, 0, 67, 41]` ships as `StdHW 0`. This pass drops
the zeros and the repeats and **keeps the order** -- index 0 is the designer's
standard stem, and sorting would hand that role to the thinnest one. It
cannot recover a master whose values are all zero -- the source has nothing
to recover -- so that case is reported as a warning rather than a repair.
"""

from __future__ import annotations

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

_FIELDS = ("postscriptStemSnapH", "postscriptStemSnapV")


def _clean(values: list) -> list:
    """Positive values, each once, in their original order."""
    return list(dict.fromkeys(value for value in values if value > 0))


def _fmt(values: list) -> str:
    return "[" + ", ".join(f"{value:g}" for value in values) + "]"


def clean_stem_snaps(ufo, master_name: str) -> list[ConversionWarning]:
    """Drop zero and repeated stems from one master's stem snap lists.

    Args:
        ufo: The in-memory UFO glyphsLib built for one master; changed in place.
        master_name: How the warnings name this master.

    Returns:
        One warning per list changed: a repair (`preflight`) when real stems
        are left, a warning when nothing but zeros was there and the field is
        now unset.
    """
    warnings: list[ConversionWarning] = []
    for field in _FIELDS:
        values = getattr(ufo.info, field, None)
        if not values:
            continue
        cleaned = _clean(values)
        if cleaned == list(values):
            continue
        setattr(ufo.info, field, cleaned or None)
        if cleaned:
            warnings.append(
                ConversionWarning(
                    category="preflight",
                    severity=WarningSeverity.INFO,
                    message=f"{field} of '{master_name}': dropped zero and repeated stems",
                    details=f"{_fmt(values)} -> {_fmt(cleaned)}",
                )
            )
        else:
            warnings.append(
                ConversionWarning(
                    category="stems",
                    severity=WarningSeverity.WARNING,
                    message=(
                        f"{field} of '{master_name}': every stem in the source is 0; "
                        "the field is left unset"
                    ),
                    details=(
                        f"Was {_fmt(values)}. The master has no stem values of its own "
                        "in the Glyphs source -- typical of UFOs merged into one Glyphs "
                        "file, where each master gets 0 for the stems the others brought."
                    ),
                )
            )
    return warnings
