# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Glyphs.app source import — converts .glyphs / .glyphspackage to UFO.

The conversion is glyphsLib's, but the writing loop is ours: it reports
per-master progress, refuses to start when it would replace UFOs the caller has
not agreed to replace, and writes no .designspace for a single-master source
(glyphsLib emits a degenerate one there — an axis whose minimum, default and
maximum are equal, with the source pinned outside it).

The result is ordinary files on disk. Unlike the binary importer, nothing lives
in a temporary directory, so the imported font saves in place like any other.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ufo_tdkit_tools.extraction.warnings import ConversionWarning, WarningSeverity

from .format4 import load_glyphs_source, source_format_version

logger = logging.getLogger(__name__)

# Glyphs 2 writes a file; Glyphs 3 can write a package, which is a directory.
GLYPHS_EXTENSIONS = (".glyphs", ".glyphspackage")

#: `(step, total, message)`. A `total` of 0 means "no count to show yet" --
#: the phases inside glyphsLib (parsing, building the designspace) take
#: minutes and report nothing, so the window pulses through those.
ProgressCallback = Callable[[int, int, str], None]


class ConversionCancelled(Exception):
    """Raised when the caller's cancel event is set during a conversion.

    Attributes:
        removed: The paths this run had written and then deleted again. A UFO
            that was being *replaced* cannot be brought back -- glyphsLib
            removes the old one before writing -- so the caller has to say so.
    """

    def __init__(self, removed: list[Path] | None = None):
        super().__init__("Conversion cancelled")
        self.removed = removed or []


@dataclass
class GlyphsSourceInfo:
    """What a Glyphs source contains, read without converting it."""

    path: Path
    family_name: str
    master_count: int
    glyph_count: int
    instance_count: int
    axes: list[tuple[str, str]] = field(default_factory=list)  # (name, tag)
    corner_component_count: int = 0
    ps_hint_count: int = 0
    tt_hint_count: int = 0
    #: The format the file was saved in. 4 is read experimentally (format4.py).
    format_version: int = 3

    @property
    def is_multi_master(self) -> bool:
        return self.master_count > 1

    @property
    def has_corner_components(self) -> bool:
        """Whether the source draws with corner parts.

        A count of -1 (`fast_inspect.COUNT_UNKNOWN`) means the source was read
        without its glyphs: the parts are there in the glyph names, but how
        often they are *placed* is not known yet. The option the dialog offers
        applies either way, so "unknown" counts as present — reading it as
        absent hid the switch on every source read the cheap way.
        """
        return self.corner_component_count != 0

    @property
    def has_ps_hints(self) -> bool:
        return self.ps_hint_count > 0


@dataclass
class GlyphsConversionResult:
    """Result of a .glyphs -> UFO conversion."""

    source_path: Path
    output_dir: Path
    ufo_paths: list[Path]
    designspace_path: Path | None
    open_path: Path  # what the application should open: the DS, or the lone UFO
    warnings: list[ConversionWarning] = field(default_factory=list)
    glyph_count: int = 0
    master_count: int = 0
    corners_applied: int = 0
    ps_hints_imported: int = 0
    tt_deltas_restored: int = 0

    @property
    def is_designspace(self) -> bool:
        return self.designspace_path is not None


def is_glyphs_source(path: "Path | str") -> bool:
    """True when `path` names a Glyphs source by extension."""
    return Path(path).suffix.lower() in GLYPHS_EXTENSIONS


def default_output_dir(source_path: "Path | str") -> Path:
    """The directory an import writes to unless the user picks another one.

    A folder named after the source, beside it — `Family.glyphs` gives
    `Family/`, which cannot collide with the source itself.
    """
    source = Path(source_path).resolve()
    return source.parent / source.stem


def inspect_glyphs_source(source_path: "Path | str") -> GlyphsSourceInfo:
    """Read a Glyphs source and report what it holds, without converting.

    Parsing is the expensive half of a conversion (roughly 0.03 s per megabyte),
    so this is cheap enough to run when a dialog opens — but it is still I/O and
    belongs in a worker thread for a large source.

    Raises:
        FileNotFoundError: the path does not exist.
        Exception: whatever glyphsLib raises for an unreadable source.
    """

    source = Path(source_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Glyphs source not found: {source}")

    from .preflight import count_component_hints, count_hints_by_family

    font = load_glyphs_source(source)
    ps_hints, tt_hints = count_hints_by_family(font)
    return GlyphsSourceInfo(
        path=source,
        family_name=font.familyName or source.stem,
        master_count=len(font.masters),
        glyph_count=len(font.glyphs),
        instance_count=len(font.instances),
        axes=[(a.name, a.axisTag) for a in font.axes],
        corner_component_count=count_component_hints(font),
        ps_hint_count=ps_hints,
        tt_hint_count=tt_hints,
        format_version=source_format_version(font),
    )


def _delta_locations(gs_font) -> set[str]:
    """Every design-space location a TrueType delta is authored at.

    Glyphs writes deltas per *instance*, so these are usually not master
    locations — which is why a source can be full of deltas and still hand a
    master none.
    """
    locations: set[str] = set()
    for glyph in gs_font.glyphs:
        for layer in glyph.layers:
            for hint in getattr(layer, "hints", []):
                if str(getattr(hint, "type", "")) != "TTDelta":
                    continue
                for by_location in (getattr(hint, "settings", None) or {}).values():
                    if isinstance(by_location, dict):
                        locations.update(str(k) for k in by_location)
    return locations


def _designspace_name(source: Path) -> str:
    return f"{source.stem}.designspace"


def plan_output_paths(
    source_path: "Path | str",
    output_dir: "Path | str",
) -> list[Path]:
    """The paths a conversion would write, so a caller can check for collisions.

    Reads the source to learn the master names — the UFO filenames come from
    glyphsLib, not from a naming rule we could reproduce here.

    Returns:
        UFO paths first, then the .designspace path when one would be written.
    """
    from glyphsLib import to_designspace

    source = Path(source_path).resolve()
    out_dir = Path(output_dir)

    font = load_glyphs_source(source)
    doc = to_designspace(font, write_skipexportglyphs=True)

    paths = [out_dir / name for name in _unique_source_filenames(doc)]
    if len(doc.sources) > 1:
        paths.append(out_dir / _designspace_name(source))
    return paths


def _unique_source_filenames(doc) -> list[str]:
    """Source filenames in document order, without repeats.

    Layer sources — Glyphs brace layers — are separate designspace sources that
    share one UFO with the master they sit between, so the same filename appears
    more than once and must be written only once.
    """
    seen: list[str] = []
    for source in doc.sources:
        if source.filename not in seen:
            seen.append(source.filename)
    return seen


def _validate_output_dir(source: Path, out_dir: Path) -> None:
    """Refuse a destination that would write into the source itself."""
    if out_dir == source:
        raise ValueError("Output directory cannot be the Glyphs source itself")
    if source.is_dir() and out_dir.is_relative_to(source):
        raise ValueError("Output directory cannot be inside the Glyphs package")


class _WarningCollector(logging.Handler):
    """Turns glyphsLib's own log output into conversion warnings.

    glyphsLib reports what it could not carry over by logging it; there is no
    other channel. Records below WARNING are the running commentary of a normal
    conversion ("Running 'propagate_all_anchors'") and are dropped.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings: list[ConversionWarning] = []

    def emit(self, record: logging.LogRecord) -> None:
        severity = (
            WarningSeverity.ERROR if record.levelno >= logging.ERROR else WarningSeverity.WARNING
        )
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not break the import
            message = str(record.msg)
        self.warnings.append(
            ConversionWarning(
                category="glyphslib",
                severity=severity,
                message=message,
            )
        )


def _remove_written(paths: list[Path], out_dir: Path, created_dir: bool) -> list[Path]:
    """Undo what this run wrote, and nothing else.

    Only the paths this conversion created are touched: a destination folder
    the user already had keeps everything else in it, and is itself removed
    only when this run made it and left it empty.
    """
    import shutil

    removed: list[Path] = []
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                continue
            removed.append(path)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not remove %s: %s", path, exc)

    if created_dir and out_dir.is_dir():
        try:
            out_dir.rmdir()  # refuses when anything else is in there
        except OSError:
            pass
    return removed


def _repair_summary(warnings: list[ConversionWarning]) -> list[str]:
    """One line per kind of repair, for the progress journal.

    A real source produces thousands of these -- 245 renamed layers, 211
    dangling components -- and a journal that lists them one by one tells the
    reader nothing they can act on.
    """
    import re
    from collections import Counter

    counts: Counter[str] = Counter()
    for warning in warnings:
        if warning.category != "preflight":
            continue
        kind = re.sub(r"'[^']*'", "'...'", re.sub(r"\d+", "N", warning.message))
        kind = kind.split(":")[-1].strip() or kind
        counts[kind] += 1

    return [f"Repaired {count}x: {kind}" for kind, count in counts.most_common()]


def convert_glyphs_to_ufos(
    source_path: "Path | str",
    output_dir: "Path | str",
    progress_callback: ProgressCallback | None = None,
    apply_corners: bool = False,
    font: "Any | None" = None,
    cancel: "Any | None" = None,
) -> GlyphsConversionResult:
    """Convert a Glyphs source into UFO masters, and a DesignSpace if it has several.

    Existing UFOs at the destination are **replaced** — glyphsLib's `clean_ufo`
    removes the old directory before writing, so a stale glyph file cannot
    survive into the new master. Check `plan_output_paths()` and confirm with the
    user before calling this.

    Args:
        source_path: Path to a .glyphs file or .glyphspackage directory.
        output_dir: Directory to write the masters into; created if absent.
        progress_callback: Called as `(step, total, message)`. `total` is 0
            while the work has no countable items -- reading the source and
            building the designspace are single long steps inside glyphsLib.
        font: An already-parsed `GSFont` to convert instead of reading the
            source again. **The converter takes ownership and mutates it**:
            preflight repairs it in place and `to_designspace` consumes it, so
            it is good for exactly one conversion.
        cancel: Anything with `is_set()` -- a `threading.Event`, say. Polled
            between phases and between masters; when set, whatever this run
            wrote is removed and `ConversionCancelled` is raised.
        apply_corners: Bake corner and cap components into the outlines, the way
            Glyphs draws and exports them. One-way: the corner stops being a
            component.

    Returns:
        GlyphsConversionResult; `open_path` is the .designspace for a
        multi-source font and the single UFO otherwise.

    Raises:
        FileNotFoundError: the source does not exist.
        ValueError: the destination would write into the source.
        ConversionCancelled: the cancel event was set.
        Exception: whatever glyphsLib raises for an unreadable source.
    """
    from glyphsLib import clean_ufo, to_designspace

    from .axis_maps import repair_axis_maps
    from .corners import apply_corner_components
    from .preflight import preflight_source
    from .ps_hints import import_ps_hints as import_ps_hints_into
    from .ps_hints import reanchor_ps_hints
    from .stem_snaps import clean_stem_snaps
    from .tt_hints import restore_tt_delta_settings

    source = Path(source_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Glyphs source not found: {source}")

    out_dir = Path(output_dir).resolve()
    _validate_output_dir(source, out_dir)

    collector = _WarningCollector()
    glyphs_logger = logging.getLogger("glyphsLib")
    glyphs_logger.addHandler(collector)
    try:
        written_paths: list[Path] = []
        created_dir = not out_dir.exists()

        def _stop_if_cancelled() -> None:
            """Raise if the caller asked to stop, taking this run's files with us.

            glyphsLib's parse and build cannot be interrupted, so a cancel
            during them is noticed here, at the next checkpoint -- which is why
            the window keeps its clock running and says "Cancelling".
            """
            if cancel is None or not cancel.is_set():
                return
            removed = _remove_written(written_paths, out_dir, created_dir)
            raise ConversionCancelled(removed)

        _stop_if_cancelled()

        if font is None:
            if progress_callback:
                progress_callback(0, 0, f"Reading {source.name}")
            font = load_glyphs_source(source)
        _stop_if_cancelled()

        glyph_count = len(font.glyphs)
        master_count = len(font.masters)

        # Repair what glyphsLib refuses to convert. This parse is ours alone,
        # so the source file on disk is untouched.
        if progress_callback:
            progress_callback(0, 0, "Repairing the source")
        preflight_warnings = preflight_source(font)
        if progress_callback:
            for line in _repair_summary(preflight_warnings):
                progress_callback(0, 0, line)
        _stop_if_cancelled()

        if progress_callback:
            progress_callback(0, 0, f"Building the designspace ({master_count} masters)")
        doc = to_designspace(font, write_skipexportglyphs=True)
        axis_warnings = repair_axis_maps(doc)
        if progress_callback:
            for warning in axis_warnings:
                progress_callback(0, 0, warning.message)
        filenames = _unique_source_filenames(doc)
        _stop_if_cancelled()

        out_dir.mkdir(parents=True, exist_ok=True)

        ufo_paths: list[Path] = []
        written: dict[str, object] = {}
        corners_applied = 0
        ps_hints_imported = 0
        tt_deltas_restored = 0
        masters_without_deltas: list[str] = []
        stem_warnings: list[ConversionWarning] = []
        for source_entry in doc.sources:
            if source_entry.filename in written:
                continue
            ufo_path = out_dir / source_entry.filename

            _stop_if_cancelled()

            if progress_callback:
                verb = "Replacing" if ufo_path.exists() else "Writing"
                progress_callback(
                    len(written),
                    len(filenames),
                    f"{verb} {source_entry.filename}",
                )

            # Both passes run here, while the font is still ufoLib2 and still in
            # memory — the corner filter accepts nothing else, and neither pass
            # should ever rewrite a UFO already on disk.
            #
            # Hand-made PostScript hints come across unconditionally, the way
            # the binary importer brings CFF hints across: writing them adds a
            # lib key and costs nothing when there are none, so there is nothing
            # for the user to decide. They are read BEFORE the corners are
            # baked: a ghost hint names its node by index, and baking inserts
            # points, so afterwards the index lands on a different node (a top
            # ghost at 700 came out at 0).
            ps_hints_imported += import_ps_hints_into(source_entry.font)

            if apply_corners:
                corners_applied += apply_corner_components(source_entry.font)
                reanchor_ps_hints(source_entry.font)

            # TrueType delta amounts are the one field glyphsLib drops; put it
            # back before the master is written.
            restored, master_found = restore_tt_delta_settings(font, source_entry.font)
            tt_deltas_restored += restored
            if not master_found:
                masters_without_deltas.append(source_entry.filename)

            # Masters merged from several UFOs carry 0 for every stem another
            # master brought; glyphsLib copies those zeros into the stem snaps.
            stem_warnings += clean_stem_snaps(source_entry.font, source_entry.filename)

            clean_ufo(str(ufo_path))
            source_entry.font.save(str(ufo_path))

            written[source_entry.filename] = source_entry.font
            ufo_paths.append(ufo_path)
            written_paths.append(ufo_path)

        # Say so when TrueType deltas were present but none applied to any
        # master — Glyphs authors deltas per instance, so a master can sit at a
        # location no delta set was written for, and a silently empty delta
        # would be indistinguishable from a delta that does nothing.
        extra_warnings: list[ConversionWarning] = list(stem_warnings)
        if masters_without_deltas:
            extra_warnings.append(
                ConversionWarning(
                    category="preflight",
                    severity=WarningSeverity.WARNING,
                    message=(
                        f"{len(masters_without_deltas)} master(s) could not be matched to "
                        "a Glyphs master; their TrueType delta amounts were not restored"
                    ),
                    details=", ".join(masters_without_deltas),
                )
            )
        else:
            delta_locations = _delta_locations(font)
            if tt_deltas_restored == 0 and delta_locations:
                from .tt_hints import master_location_key

                extra_warnings.append(
                    ConversionWarning(
                        category="preflight",
                        severity=WarningSeverity.INFO,
                        message=(
                            "TrueType delta hints are defined at instance locations that "
                            "no master shares, so no delta amounts were carried over"
                        ),
                        details=(
                            "Deltas at: "
                            + ", ".join(sorted(delta_locations))
                            + "; masters at: "
                            + ", ".join(master_location_key(m) for m in font.masters)
                        ),
                    )
                )

        # A single source means a single master with no interpolation to
        # describe. glyphsLib still produces a document for it, but a degenerate
        # one, so it is worse than no document at all.
        designspace_path: Path | None = None
        if len(doc.sources) > 1:
            designspace_path = out_dir / _designspace_name(source)
            if progress_callback:
                progress_callback(
                    len(ufo_paths), len(filenames), f"Writing {designspace_path.name}"
                )
            doc.write(str(designspace_path))
            written_paths.append(designspace_path)

        open_path = designspace_path if designspace_path is not None else ufo_paths[0]

        if progress_callback:
            progress_callback(len(filenames), len(filenames), f"Wrote {len(ufo_paths)} masters")

        logger.info(
            "Converted %s -> %d UFO(s) in %s%s",
            source.name,
            len(ufo_paths),
            out_dir,
            " + designspace" if designspace_path else "",
        )

        return GlyphsConversionResult(
            source_path=source,
            output_dir=out_dir,
            ufo_paths=ufo_paths,
            designspace_path=designspace_path,
            open_path=open_path,
            # Preflight repairs first: they describe what the source needed
            # before glyphsLib would look at it.
            warnings=preflight_warnings + axis_warnings + extra_warnings + collector.warnings,
            glyph_count=glyph_count,
            master_count=master_count,
            corners_applied=corners_applied,
            ps_hints_imported=ps_hints_imported,
            tt_deltas_restored=tt_deltas_restored,
        )
    finally:
        glyphs_logger.removeHandler(collector)


def format_source_summary(info: GlyphsSourceInfo) -> str:
    """One line describing a source, for a dialog heading."""
    masters = f"{info.master_count} master" + ("s" if info.master_count != 1 else "")
    glyphs = f"{info.glyph_count} glyph" + ("s" if info.glyph_count != 1 else "")
    parts = [masters, glyphs]
    if info.axes:
        # An axis can carry a name and no tag — show whichever it has rather
        # than an empty slot in the list.
        labels = [tag or name for name, tag in info.axes if (tag or name)]
        if labels:
            parts.append("axes " + ", ".join(labels))
    # A count of -1 means the source was read the cheap way and the hints were
    # never looked at: they live in the glyphs. Saying nothing beats "-1".
    if info.ps_hint_count > 0:
        parts.append(f"{info.ps_hint_count} PS hints")
    if info.format_version >= 4:
        parts.append("Glyphs 4 format (experimental)")
    if info.instance_count:
        instances = f"{info.instance_count} instance" + ("s" if info.instance_count != 1 else "")
        parts.append(instances)
    return " · ".join(parts)


def describe_existing(paths: list[Path]) -> list[Path]:
    """The subset of `paths` that already exists on disk."""
    return [p for p in paths if os.path.exists(p)]
