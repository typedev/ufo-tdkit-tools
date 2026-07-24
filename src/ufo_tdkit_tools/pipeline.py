# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Single entry point for the OTF/UFO -> OTF+UFO hint pipeline.

Handles the full flow: input dispatch (binary vs UFO), hint source resolution,
optional optimization, normalization to default-layer ``autohint.v2``, and
compilation to OTF.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ufo_tdkit_tools.constants import PROCESSED_LAYER_NAME

logger = logging.getLogger(__name__)


HintSourceArg = Literal["auto", "processed", "v2", "public_ps"]
AutohintArg = Literal["fill", "all", "off"]
_BINARY_SUFFIXES = frozenset({".otf", ".ttf", ".woff", ".woff2"})
_HASHMAP_DATA_NAME = "com.adobe.type.processedHashMap"


@dataclass
class ProcessResult:
    """Result of :func:`process_font`."""

    success: bool
    hint_source_used: str | None = None
    glyphs_total: int = 0
    glyphs_with_hints: int = 0
    optimized: bool = False
    optimized_count: int = 0
    autohinted: bool = False
    autohinted_count: int = 0
    otf_glyphs_hinted: int = 0
    otf_glyphs_total: int = 0
    error: str | None = None


def process_font(
    input_path: Path | str,
    output_otf: Path | str,
    output_ufo: Path | str,
    *,
    hint_source: HintSourceArg = "auto",
    autohint: AutohintArg = "fill",
    optimize: bool = False,
    tx_path: str | None = None,
    makeotf_path: str | None = None,
    logger_: logging.Logger | None = None,
) -> ProcessResult:
    """Convert any input (binary or UFO) to a hinted OTF + UFO pair.

    For binary inputs (``.otf``/``.ttf``/``.woff``/``.woff2``), hints are
    extracted from CFF charstrings into the UFO default-layer
    ``com.adobe.type.autohint.v2`` lib key.

    For UFO inputs, the hint source is selected via ``hint_source``:

    - ``auto``: whole-font priority ``PROCESSED_LAYER > AUTOHINT_V2 > PUBLIC_PS``;
      the first source containing hints in at least one glyph wins.
    - ``processed`` / ``v2`` / ``public_ps``: force the given source.
      If the font has no hints in that source at all, returns failure.

    For binary inputs, hints are extracted from CFF charstrings.

    ``autohint`` decides what happens to glyphs the chosen source does not
    cover -- the common case for hand-hinted masters, where only base forms
    carry authored hints:

    - ``fill`` (default): run ``afdko.otfautohint`` on exactly those glyphs and
      keep the authored hints of the others. A font with no hints anywhere is
      hinted in full, which is also what happens for a binary input whose CFF
      carried none.
    - ``all``: ignore authored hints and re-hint every drawable glyph.
    - ``off``: leave unhinted glyphs unhinted. A source with no hints at all
      then fails instead of falling back to the autohinter.

    ``ProcessResult.autohinted`` records whether the autohinter ran at all and
    ``autohinted_count`` how many glyphs it hinted.

    With ``optimize=True``, hints are routed through the processedglyphs layer
    for the optimizer, then exported back to default ``autohint.v2``.

    Layer hygiene: if the input UFO did not contain a processedglyphs layer
    but the pipeline created one as a working buffer, it is removed before
    saving (together with the autohinter's ``processedHashMap`` data file).

    Args:
        input_path: Path to ``.otf``/``.ttf``/``.woff``/``.woff2`` or ``.ufo``.
        output_otf: Path for the output hinted OTF.
        output_ufo: Path for the output UFO.
        hint_source: Source selection for UFO inputs (ignored for binary).
        autohint: What to do with glyphs the source does not hint --
            ``"fill"``, ``"all"`` or ``"off"``.
        optimize: Run the ps_hints optimizer before compilation.
        tx_path: Absolute path to the ``tx`` binary, forwarded to the
            preserve-optimized compiler. Needed when the caller runs in a
            subprocess whose PATH lacks AFDKO (e.g. a ProcessPoolExecutor
            worker). Falls back to PATH lookup when None.
        makeotf_path: Absolute path to the ``makeotf`` binary, same rationale.
        logger_: Optional logger; defaults to module logger.

    Returns:
        :class:`ProcessResult` with success flag and statistics.
    """
    log = logger_ or logger
    if autohint not in ("fill", "all", "off"):
        return ProcessResult(success=False, error=f"unknown autohint mode: {autohint!r}")
    input_path = Path(input_path).resolve()
    output_otf = Path(output_otf).resolve()
    output_ufo = Path(output_ufo).resolve()

    if not input_path.exists():
        return ProcessResult(success=False, error=f"Input not found: {input_path}")

    suffix = input_path.suffix.lower()
    is_binary = suffix in _BINARY_SUFFIXES
    is_ufo = input_path.is_dir() and suffix == ".ufo"
    if not is_binary and not is_ufo:
        return ProcessResult(
            success=False,
            error=f"Unsupported input format: {input_path}",
        )

    output_otf.parent.mkdir(parents=True, exist_ok=True)
    output_ufo.parent.mkdir(parents=True, exist_ok=True)

    work_dir = tempfile.TemporaryDirectory(prefix="ufo_tdkit_pipeline_")
    extract_temp = None
    font = None
    try:
        font, extract_temp = _load_input(input_path, work_dir.name, is_binary)

        from ufo_tdkit_tools.ps_hints.batch import (
            count_glyphs_with_source,
            export_all_from_processed,
            glyphs_missing_source,
            import_all_to_processed,
            optimize_font,
            remove_all_hints,
        )
        from ufo_tdkit_tools.ps_hints.parser import HintSource

        had_processed_layer = PROCESSED_LAYER_NAME in [layer.name for layer in font.layers]

        if autohint == "all":
            # Authored hints are discarded wholesale; clearing v2 keeps stale
            # entries out of the output for anything the autohinter skips.
            source = None
            remove_all_hints(font, "v2")
        else:
            source = _resolve_source(font, hint_source, is_binary, log)
            if source is None and not is_binary and hint_source != "auto":
                return ProcessResult(
                    success=False,
                    error=f"No hints found for hint_source={hint_source!r}",
                )
        if source is None and autohint == "off":
            return ProcessResult(
                success=False,
                error="No hints found in any source and autohint is off",
            )

        # Glyphs the source does not cover. A partially hinted source is the
        # norm for hand-hinted masters, so this is a per-glyph question, not a
        # whole-font one.
        missing = (
            glyphs_missing_source(font, source) if autohint != "off" else []
        )

        glyphs_total = len(font)
        glyphs_with_hints = count_glyphs_with_source(font, source) if source else 0
        log.info(
            f"pipeline: source={source.value if source else None} glyphs={glyphs_total} "
            f"with_hints={glyphs_with_hints} to_autohint={len(missing)}"
        )

        autohinted = False
        autohinted_count = 0
        optimized_count = 0

        if missing or optimize:
            # Buffer route: everything meets in the processedglyphs layer, gets
            # optionally optimized there, and is exported back to default v2.
            if source is not None:
                imported = import_all_to_processed(font, source)
                log.info(f"pipeline: imported {imported} glyphs to processedglyphs")

            if missing:
                font = _autohint_glyphs(font, missing, log)
                autohinted = True
                still_missing = set(
                    glyphs_missing_source(font, HintSource.PROCESSED_LAYER)
                )
                autohinted_count = sum(1 for n in missing if n not in still_missing)
                log.info(
                    f"pipeline: autohinted {autohinted_count}/{len(missing)} "
                    "unhinted glyphs"
                )

            if optimize:
                stats = optimize_font(font)
                optimized_count = stats["optimized"]
                log.info(
                    f"pipeline: optimized={stats['optimized']} skipped={stats['skipped']}"
                )
            export_all_from_processed(font, "v2")
        elif source == HintSource.PROCESSED_LAYER:
            export_all_from_processed(font, "v2")
        elif source == HintSource.PUBLIC_PS:
            import_all_to_processed(font, source)
            export_all_from_processed(font, "v2")
        # source == AUTOHINT_V2 with nothing to fill: already where it belongs.

        source_used = source or HintSource.PROCESSED_LAYER

        drop_processed_layer = not had_processed_layer
        if drop_processed_layer:
            current = [layer.name for layer in font.layers]
            if PROCESSED_LAYER_NAME in current:
                font.removeLayer(PROCESSED_LAYER_NAME)
                log.info("pipeline: removed temporary processedglyphs layer")

        if output_ufo.exists():
            shutil.rmtree(output_ufo)
        font.save(str(output_ufo))
        if drop_processed_layer and autohinted:
            _remove_hashmap_data(output_ufo, log)
        log.info(f"pipeline: saved UFO to {output_ufo}")

        from ufo_tdkit_tools.compilation.compiler import (
            compile_otf_preserve_optimized,
        )

        compile_stats: dict[str, int] = {}
        ok = compile_otf_preserve_optimized(
            str(output_ufo),
            str(output_otf),
            logger=log,
            tx_path=tx_path,
            makeotf_path=makeotf_path,
            stats=compile_stats,
        )
        result = ProcessResult(
            success=bool(ok),
            hint_source_used=source_used.value,
            glyphs_total=glyphs_total,
            glyphs_with_hints=glyphs_with_hints,
            optimized=optimize,
            optimized_count=optimized_count,
            autohinted=autohinted,
            autohinted_count=autohinted_count,
            otf_glyphs_hinted=compile_stats.get("hints_transferred", 0),
            otf_glyphs_total=compile_stats.get("total_glyphs", 0),
        )
        if not ok:
            result.error = "OTF compilation failed"
            return result

        log.info(
            f"pipeline: compiled OTF to {output_otf} "
            f"(hinted {result.otf_glyphs_hinted}/{result.otf_glyphs_total} glyphs)"
        )
        return result
    except Exception as e:
        log.exception("pipeline: unexpected failure")
        return ProcessResult(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        if extract_temp is not None:
            try:
                extract_temp.cleanup()
            except Exception:
                pass
        work_dir.cleanup()


def _load_input(input_path: Path, work_dir: str, is_binary: bool):
    """Open input as fontParts RFont in a working directory.

    Returns ``(font, extract_temp)`` where ``extract_temp`` is the
    :class:`tempfile.TemporaryDirectory` returned by extraction (or ``None``
    for UFO inputs). Caller must ``cleanup()`` it.
    """
    if is_binary:
        from ufo_tdkit_tools.extraction.converter import convert_binary_to_ufo

        result = convert_binary_to_ufo(input_path)
        return result.font, result.temp_dir

    import fontParts.world as fp

    work_ufo = Path(work_dir) / input_path.name
    shutil.copytree(input_path, work_ufo)
    font = fp.OpenFont(str(work_ufo), showInterface=False)
    return font, None


def _autohint_glyphs(font, glyph_names: list[str], log: logging.Logger):
    """Autohint ``glyph_names`` on the working UFO and return a reloaded font.

    ``afdko.otfautohint`` works on paths, so the in-memory font (which may
    already hold an imported processedglyphs buffer) is flushed to disk first,
    hinted, and reopened. The autohinter writes only the glyphs it was asked
    for, into the processedglyphs layer, and leaves both the default layer and
    the layer's other entries alone -- so authored hints already imported into
    the buffer survive and the caller can export the union.

    ``explicitGlyphs`` is set so the autohinter does not silently skip complex
    glyphs (its ``maxSegments`` heuristic only applies to implicit selections).
    """
    import fontParts.world as fp
    from afdko.otfautohint.autohint import ACOptions, hintFiles

    font.save()
    ufo_path = str(font.path)
    log.info(f"pipeline: running otfautohint on {len(glyph_names)} glyph(s) in {ufo_path}")

    options = ACOptions()
    options.inputPaths = [ufo_path]
    options.outputPaths = [ufo_path]
    options.allowNoBlues = True
    options.glyphList = list(glyph_names)
    options.explicitGlyphs = True
    hintFiles(options)

    return fp.OpenFont(ufo_path, showInterface=False)


def _remove_hashmap_data(ufo_path: Path, log: logging.Logger) -> None:
    """Drop the autohinter's ``processedHashMap`` from a saved UFO.

    It is bookkeeping for a processedglyphs layer the pipeline has removed;
    leaving it behind would make the output UFO claim glyphs were processed by
    a layer it no longer has. Done on the saved directory rather than through
    defcon's DataSet, whose deletions are replayed against the destination of a
    save-as, where the file never existed.
    """
    data_dir = ufo_path / "data"
    hashmap = data_dir / _HASHMAP_DATA_NAME
    if not hashmap.exists():
        return
    hashmap.unlink()
    log.info("pipeline: removed temporary processedHashMap")
    try:
        data_dir.rmdir()  # only succeeds when nothing else lived there
    except OSError:
        pass


def _resolve_source(font, hint_source: str, is_binary: bool, log: logging.Logger):
    """Resolve the ``hint_source`` argument into a concrete :class:`HintSource`.

    Returns ``None`` when the requested source holds no hints at all -- for an
    explicit source that is an error, for ``auto`` and for binary inputs it
    means the autohinter has to supply everything.
    """
    from ufo_tdkit_tools.ps_hints.batch import (
        count_glyphs_with_source,
        detect_font_source,
    )
    from ufo_tdkit_tools.ps_hints.parser import HintSource

    if is_binary:
        # Hints always come from the CFF extraction; an unhinted CFF (or any
        # TTF) yields none, and hint_source is ignored either way.
        src = HintSource.AUTOHINT_V2
        return src if count_glyphs_with_source(font, src) > 0 else None

    if hint_source == "auto":
        return detect_font_source(font)

    explicit = {
        "processed": HintSource.PROCESSED_LAYER,
        "v2": HintSource.AUTOHINT_V2,
        "public_ps": HintSource.PUBLIC_PS,
    }
    src = explicit.get(hint_source)
    if src is None:
        log.error(f"unknown hint_source: {hint_source!r}")
        return None
    if count_glyphs_with_source(font, src) == 0:
        return None
    return src
