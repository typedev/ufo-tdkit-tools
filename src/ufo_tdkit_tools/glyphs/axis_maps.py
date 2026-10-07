# Copyright 2026 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Dropping an axis mapping glyphsLib built from contradictory instances.

When a source has no "Axis Location" parameters, glyphsLib builds an axis's
user -> design `<map>` from the instances: the user value from their
`weightClass` / `widthClass`, the design value from their coordinates. Glyphs
itself does not -- without "Axis Location" it exports the design coordinates as
they are. When the instances' classes contradict their coordinates (every
"Condensed" instance at a different width, say), each one redefines the last,
and the mapping that survives can send every user value to one design value.

Such a document opens, but nothing can interpolate it: the axis's range maps
to a single point, the masters fall outside it, and normalising any location
gives the same answer. This pass checks each mapping against the sources and,
where it cannot hold, replaces it with what Glyphs would export -- no mapping,
the axis spanning the masters' own coordinates.
"""

from __future__ import annotations

from typing import Any

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

_EPSILON = 1e-6


def _mapping_problem(axis, source_values: list[float], default_value: float) -> str | None:
    """Why `axis.map` cannot describe these sources, or None when it can."""
    pairs = sorted((float(i), float(o)) for i, o in axis.map)
    outputs = [o for _, o in pairs]
    for (in_a, out_a), (in_b, out_b) in zip(pairs, pairs[1:]):
        if out_b <= out_a:
            return (
                f"user {in_a:g} and {in_b:g} map to design {out_a:g} and {out_b:g}: "
                f"the mapping must rise strictly"
            )
    low, high = min(outputs), max(outputs)
    outside = sorted({v for v in source_values if v < low - _EPSILON or v > high + _EPSILON})
    if outside:
        return f"it covers design {low:g}..{high:g}, but masters sit at " + ", ".join(
            f"{v:g}" for v in outside
        )
    if abs(axis.map_forward(axis.default) - default_value) > _EPSILON:
        return (
            f"the axis default maps to design {axis.map_forward(axis.default):g}, "
            f"but the default master is at {default_value:g}"
        )
    return None


def _default_source(doc) -> Any:
    """The source glyphsLib made from the regular master: the one carrying the lib."""
    for source in doc.sources:
        if source.copyLib:
            return source
    return doc.sources[0]


def repair_axis_maps(doc) -> list[ConversionWarning]:
    """Replace every axis mapping the sources contradict; return one warning per axis.

    Args:
        doc: The `DesignSpaceDocument` from `glyphsLib.to_designspace`, changed
            in place. Source and instance locations are design coordinates and
            are left alone -- only the axes' user-facing description changes.
    """
    if not doc.sources:
        return []
    default = _default_source(doc)
    warnings: list[ConversionWarning] = []

    for axis in doc.axes:
        if not axis.map:
            continue
        values = [
            float(source.location[axis.name])
            for source in doc.sources
            if axis.name in source.location
        ]
        if not values or axis.name not in default.location:
            continue
        default_value = float(default.location[axis.name])

        problem = _mapping_problem(axis, values, default_value)
        if problem is None:
            continue

        old = ", ".join(f"{float(i):g}->{float(o):g}" for i, o in sorted(axis.map))
        axis.map = []
        axis.minimum = min(values)
        axis.maximum = max(values)
        axis.default = default_value
        warnings.append(
            ConversionWarning(
                category="preflight",
                severity=WarningSeverity.WARNING,
                message=(
                    f"Axis {axis.tag or axis.name}: dropped a user -> design mapping no master fits"
                ),
                details=(
                    f"No location on it can be interpolated: {problem}. Without an "
                    f'"Axis Mappings" parameter glyphsLib derives the mapping from the '
                    f"instances' weight/width classes, so this usually means they "
                    f"contradict the instances' coordinates. The axis now uses "
                    f"the design coordinates, {axis.minimum:g}..{axis.maximum:g} "
                    f"(default {axis.default:g}), as Glyphs exports a source without "
                    f'"Axis Location". Was: {old}.'
                ),
            )
        )
    return warnings
