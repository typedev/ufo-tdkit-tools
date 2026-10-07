# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Repairs applied to a Glyphs source before conversion.

Glyphs.app tolerates things glyphsLib refuses outright, and a production source
accumulates them: the same custom parameter written twice, a component in a
background layer pointing at a glyph that was deleted years ago. Either one
aborts the whole import with an exception naming a detail the user cannot act on.

Every repair here is applied to **our own throwaway parse** of the source — the
file on disk is never written — and every repair reports itself as a
ConversionWarning, so the import says what it changed rather than quietly
differing from the source.
"""

import logging
from typing import Any

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

from .format4 import FORMAT_NOTES_ATTR

logger = logging.getLogger(__name__)

# Hint types that are components rather than hints (see corners.py)
_COMPONENT_HINT_TYPES = frozenset({"Corner", "Cap"})


def _warn(message: str, details: str | None = None) -> ConversionWarning:
    return ConversionWarning(
        category="preflight",
        severity=WarningSeverity.WARNING,
        message=message,
        details=details,
    )


def _dedupe_custom_parameters(owner, label: str) -> list[ConversionWarning]:
    """Keep the first value of every custom parameter, drop later repeats.

    `glyphsLib.builder.custom_params.get_custom_value` raises
    `RuntimeError: More than one value for this customParameter` on the second
    occurrence, which ends the import. Glyphs itself keeps writing such files —
    `fsType` twice at font level is the common one — so the choice is between
    dropping a repeat and refusing the font.

    First wins: it is what a reader that stopped at the first match would have
    used, and it keeps the result stable if the pass runs twice.
    """
    params = list(owner.customParameters)
    if not params:
        return []

    warnings: list[ConversionWarning] = []
    seen: set[str] = set()
    kept = []
    for param in params:
        if param.name in seen:
            warnings.append(
                _warn(
                    f"{label}: duplicate custom parameter '{param.name}' dropped",
                    details=f"Kept the first value; discarded {param.value!r}.",
                )
            )
            continue
        seen.add(param.name)
        kept.append(param)

    if warnings:
        owner.customParameters = kept
    return warnings


def _dedupe_glyph_order(gs_font) -> list[ConversionWarning]:
    """Drop repeated names from the `glyphOrder` custom parameter.

    Glyphs.app writes this list through unvalidated, so a name pasted into it
    twice ships in the source. glyphsLib copies the list into
    `public.glyphOrder` verbatim, and the resulting UFO then has more order
    entries than glyphs — which fontParts refuses to read at all:

        ValueError: Duplicate glyph names are not allowed.
                    Glyph name(s) 'acutecomb' are duplicate.

    That raises inside a *getter*, so it takes down whoever merely wanted the
    display order. In the grid it produced a window with no glyphs in it, for a
    font that was otherwise perfectly loadable.

    First wins, matching `_dedupe_custom_parameters`: the second occurrence
    cannot place a glyph the first one already placed, so it carries nothing.
    """
    param = next((p for p in gs_font.customParameters if p.name == "glyphOrder"), None)
    if param is None or not isinstance(param.value, list):
        return []

    order = list(param.value)
    seen: set[str] = set()
    kept: list[str] = []
    repeats: list[str] = []
    for name in order:
        if name in seen:
            if name not in repeats:
                repeats.append(name)
            continue
        seen.add(name)
        kept.append(name)

    if not repeats:
        return []

    param.value = kept
    return [
        _warn(
            f"Font: glyphOrder listed {len(repeats)} name(s) more than once — repeats dropped",
            details=(
                f"{', '.join(repeats)}. Kept each name's first position; "
                f"{len(order)} entries became {len(kept)}."
            ),
        )
    ]


def _remove_missing_components(gs_font) -> list[ConversionWarning]:
    """Drop components that reference a glyph the font does not contain.

    glyphsLib raises `MissingComponentError` when it tries to draw one, which
    ends the import. These live mostly in background layers, where a designer's
    older version of a glyph keeps referring to a part that was since renamed.

    ⚠ `GSLayer.background` is a **property that creates** a background layer when
    none exists (`glyphsLib/classes.py:3893`). Walking it here once turned a
    source that imported cleanly into `KeyError: "glyph named 'B' already
    exists"`, because every layer in the font grew an empty background. Read
    `_background`, which is None when there is none.
    """
    warnings: list[ConversionWarning] = []
    known = {glyph.name for glyph in gs_font.glyphs}

    for glyph in gs_font.glyphs:
        for layer in glyph.layers:
            holders = ((layer, "layer"), (getattr(layer, "_background", None), "background"))
            for holder, where in holders:
                if holder is None:
                    continue
                missing = [c for c in holder.components if c.name not in known]
                for component in missing:
                    layer_name = layer.name or layer.layerId
                    warnings.append(
                        _warn(
                            f"Glyph '{glyph.name}': component '{component.name}' "
                            f"points to a glyph that does not exist — removed",
                            details=f"In the {where} of layer '{layer_name}'.",
                        )
                    )
                    holder.components.remove(component)

    return warnings


def _rename_layers_with_colliding_backgrounds(gs_font) -> list[ConversionWarning]:
    """Give two layers of one master distinct names, so their backgrounds fit.

    glyphsLib names a background layer after the layer it belongs to --
    `public.background` for a master layer, `<layer name>.background` for any
    other (`builder/layers.py:71`). Two layers of one master sharing a name
    therefore claim the same background layer, and the second `newGlyph` ends
    the import with `KeyError: "glyph named 'A' already exists"`.

    Glyphs lets a designer keep several manual backups of a glyph, and they are
    named by the date they were taken -- so two backups made in the same minute
    carry the same name. A large production source had 241 of these, across 216
    masters, and the import died on the first.

    glyphsLib already solves this for the *outlines*: it warns and appends
    ` #1`, ` #2` (`builder/layers.py:46-59`). It simply does not do the same for
    backgrounds. So this does what glyphsLib does, with the same names, before
    the conversion starts -- nothing is dropped, and a round trip back to
    Glyphs gives the backups distinct names instead of losing one's background.

    ⚠ Read `_background`: `GSLayer.background` is a property that *creates* one.
    """
    warnings: list[ConversionWarning] = []

    for glyph in gs_font.glyphs:
        # The name a background would be written under, per master.
        taken: dict[tuple[str, str], object] = {}
        for layer in glyph.layers:
            if getattr(layer, "_background", None) is None:
                continue
            if layer.layerId == layer.associatedMasterId:
                continue  # the master layer owns public.background; unique already

            key = (layer.associatedMasterId, layer.name or "")
            if key not in taken:
                taken[key] = layer
                continue

            base = layer.name or layer.layerId
            suffix = 1
            while (layer.associatedMasterId, f"{base} #{suffix}") in taken:
                suffix += 1
            new_name = f"{base} #{suffix}"
            layer.name = new_name
            taken[(layer.associatedMasterId, new_name)] = layer
            warnings.append(
                _warn(
                    f"Glyph '{glyph.name}': renamed a second layer called '{base}' to '{new_name}'",
                    details=(
                        "Two layers of one master had the same name, so their "
                        "backgrounds would have been written to the same UFO layer. "
                        "glyphsLib renames duplicate outline layers the same way."
                    ),
                )
            )

    return warnings


class UnwritableOutlineError(ValueError):
    """A contour in an exported layer cannot be written to a UFO.

    Raised by `preflight_source()` before the conversion starts, instead of
    `GlifLibError` minutes later while writing the master.
    """


def _cubic_runs(path) -> list[tuple[list, Any]]:
    """Every off-curve run longer than two that ends on a `curve` point.

    Returns `(run, curve_node)` pairs. A closed path is walked from just after
    its last on-curve point, so a run that wraps from the end of the node list
    to its start is seen whole -- Glyphs keeps the start point last, and
    reading the list front to back splits such a run in two.
    """
    nodes = list(path.nodes)
    if path.closed:
        on_curve = [i for i, node in enumerate(nodes) if node.type != "offcurve"]
        if not on_curve:
            return []
        last = on_curve[-1]
        nodes = nodes[last + 1 :] + nodes[: last + 1]

    runs: list[tuple[list, Any]] = []
    run: list = []
    for node in nodes:
        if node.type == "offcurve":
            run.append(node)
            continue
        if node.type == "curve" and len(run) > 2:
            runs.append((run, node))
        run = []
    return runs


def _is_exported_layer(layer) -> bool:
    """A master, brace or bracket layer -- one that becomes a designspace source."""
    return bool(layer._is_master_layer or layer._is_brace_layer() or layer._is_bracket_layer())


def _repair_degenerate_cubic_runs(gs_font) -> list[ConversionWarning]:
    """Make every cubic segment writable: at most two off-curves before a curve point.

    A UFO cubic segment carries at most two off-curve points; the GLIF writer
    refuses a third with `too many offcurve points before curve point`, and the
    import ends after minutes of work. Glyphs tolerates it, so such contours sit
    in sources unnoticed for years.

    Where they come from is visible in the coordinates. A large production source has
    four of them, all in backup layers of one glyph, and each is a run like

        line (1373, 1219) -> offcurve x3 at (1373, 1219) -> curve (1373, 1219)

    -- a zero-length segment whose off-curves are copies of the point they sit
    on. Dropping the duplicates leaves the outline exactly as it was drawn.

    A run of genuinely distinct off-curves is a curve no UFO can hold, and
    guessing which point to throw away would change the shape. Where the
    contour is only reference drawing -- a background, or a backup layer that is
    neither a master nor a brace/bracket layer -- the whole contour is removed
    and reported. In an exported layer it would change the font, so the import
    stops here with `UnwritableOutlineError` naming every such contour.

    Quadratic runs are not touched: a `qcurve` may have any number of
    off-curves, and this source has 92 thousand of them.
    """
    warnings: list[ConversionWarning] = []
    fatal: list[str] = []

    for glyph in gs_font.glyphs:
        for layer in glyph.layers:
            layer_name = layer.name or layer.layerId
            holders = ((layer, "layer"), (getattr(layer, "_background", None), "background"))
            for holder, where in holders:
                if holder is None:
                    continue
                for path in list(holder.paths):
                    runs = _cubic_runs(path)
                    if not runs:
                        continue
                    remove: list = []
                    distinct = 0
                    for run, curve in runs:
                        # Keep the two closest to the curve point, and drop
                        # only those of the rest that add nothing.
                        surplus = run[:-2]
                        coincident = [
                            n
                            for n in surplus
                            if (n.position.x, n.position.y) == (curve.position.x, curve.position.y)
                        ]
                        if len(coincident) == len(surplus):
                            remove.extend(coincident)
                        else:
                            distinct = max(distinct, len(run))

                    if distinct:
                        if where == "layer" and _is_exported_layer(layer):
                            fatal.append(f"'{glyph.name}', layer '{layer_name}'")
                            continue
                        holder.shapes.remove(path)
                        warnings.append(
                            _warn(
                                f"Glyph '{glyph.name}': removed a contour whose cubic "
                                f"segment has {distinct} off-curve points",
                                details=(
                                    f"In the {where} of layer '{layer_name}'. A UFO cubic "
                                    f"segment holds two off-curves and these points differ, "
                                    f"so the contour cannot be written; it is reference "
                                    f"drawing, not part of the font."
                                ),
                            )
                        )
                        continue

                    for node in remove:
                        path.nodes.remove(node)
                    warnings.append(
                        _warn(
                            f"Glyph '{glyph.name}': removed "
                            f"{len(remove)} duplicate off-curve point(s) "
                            f"from a zero-length segment",
                            details=(
                                f"In the {where} of layer '{layer_name}'. A UFO "
                                f"cubic segment holds two off-curves; these were "
                                f"copies of the point they sat on."
                            ),
                        )
                    )

    if fatal:
        raise UnwritableOutlineError(
            "A cubic segment has more than two off-curve points, which a UFO cannot "
            "hold, in " + "; ".join(fatal) + ". Repair the contour in Glyphs and "
            "import again."
        )
    return warnings


def preflight_source(gs_font) -> list[ConversionWarning]:
    """Repair a parsed Glyphs source in place so glyphsLib can convert it.

    Args:
        gs_font: A `GSFont` — must be a parse the caller owns, since it is
            mutated.

    Returns:
        One warning per repair, in the order they were applied. An empty list
        means the source needed nothing.

    Raises:
        UnwritableOutlineError: an exported layer has a contour no UFO can
            hold; repairing it would change the font.
    """
    warnings: list[ConversionWarning] = []

    # What reading a Glyphs 4 source rewrote (format4.py) leads the report.
    warnings.extend(getattr(gs_font, FORMAT_NOTES_ATTR, None) or [])
    warnings.extend(_dedupe_custom_parameters(gs_font, "Font"))
    warnings.extend(_dedupe_glyph_order(gs_font))
    for master in gs_font.masters:
        warnings.extend(_dedupe_custom_parameters(master, f"Master '{master.name}'"))
    for instance in gs_font.instances:
        warnings.extend(_dedupe_custom_parameters(instance, f"Instance '{instance.name}'"))

    warnings.extend(_remove_missing_components(gs_font))
    warnings.extend(_rename_layers_with_colliding_backgrounds(gs_font))
    warnings.extend(_repair_degenerate_cubic_runs(gs_font))

    if warnings:
        logger.info("Preflight repaired %d problem(s) in the Glyphs source", len(warnings))
    return warnings


def count_component_hints(gs_font) -> int:
    """How many corner/cap components the source actually uses.

    Counted from the hints rather than from the `_corner.*` glyphs, because a
    part can be present and never placed.
    """
    total = 0
    for glyph in gs_font.glyphs:
        for layer in glyph.layers:
            for hint in getattr(layer, "hints", []):
                if str(getattr(hint, "type", "")) in _COMPONENT_HINT_TYPES:
                    total += 1
    return total


def count_hints_by_family(gs_font) -> tuple[int, int]:
    """PostScript and TrueType hint counts in the source.

    Returns:
        (ps_hint_count, tt_hint_count) — PostScript being Stem and the two
        ghost types, TrueType being everything whose type name starts with TT.
    """
    ps = tt = 0
    for glyph in gs_font.glyphs:
        for layer in glyph.layers:
            for hint in getattr(layer, "hints", []):
                hint_type = str(getattr(hint, "type", ""))
                if hint_type in ("Stem", "TopGhost", "BottomGhost"):
                    ps += 1
                elif hint_type.startswith("TT"):
                    tt += 1
    return ps, tt
