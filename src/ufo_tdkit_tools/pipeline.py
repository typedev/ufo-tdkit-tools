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
_BINARY_SUFFIXES = frozenset({".otf", ".ttf", ".woff", ".woff2"})


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
    error: str | None = None


def process_font(
    input_path: Path | str,
    output_otf: Path | str,
    output_ufo: Path | str,
    *,
    hint_source: HintSourceArg = "auto",
    optimize: bool = False,
    logger_: logging.Logger | None = None,
) -> ProcessResult:
    """Convert any input (binary or UFO) to a hinted OTF + UFO pair.

    For binary inputs (``.otf``/``.ttf``/``.woff``/``.woff2``), hints are
    extracted from CFF charstrings into the UFO default-layer
    ``com.adobe.type.autohint.v2`` lib key.

    For UFO inputs, the hint source is selected via ``hint_source``:

    - ``auto``: whole-font priority ``PROCESSED_LAYER > AUTOHINT_V2 > PUBLIC_PS``;
      the first source containing hints in at least one glyph wins. If the UFO
      has no hints in any source, ``afdko.otfautohint`` is run on the working
      UFO and its output is used (``ProcessResult.autohinted = True``).
    - ``processed`` / ``v2`` / ``public_ps``: force the given source.
      If the font has no hints in that source at all, returns failure.

    For binary inputs, hints are extracted from CFF charstrings; if extraction
    yields no hints (e.g. an unhinted CFF or any TTF), ``afdko.otfautohint``
    is run on the extracted UFO before compilation.

    Glyphs without hints in the chosen source land in the output without hints.

    With ``optimize=True``, hints are routed through the processedglyphs layer
    for the optimizer, then exported back to default ``autohint.v2``.

    Layer hygiene: if the input UFO did not contain a processedglyphs layer
    but the pipeline created one as a working buffer, it is removed before
    saving.

    Args:
        input_path: Path to ``.otf``/``.ttf``/``.woff``/``.woff2`` or ``.ufo``.
        output_otf: Path for the output hinted OTF.
        output_ufo: Path for the output UFO.
        hint_source: Source selection for UFO inputs (ignored for binary).
        optimize: Run the ps_hints optimizer before compilation.
        logger_: Optional logger; defaults to module logger.

    Returns:
        :class:`ProcessResult` with success flag and statistics.
    """
    log = logger_ or logger
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
            detect_font_source,
            export_all_from_processed,
            import_all_to_processed,
            optimize_font,
        )
        from ufo_tdkit_tools.ps_hints.parser import HintSource

        had_processed_layer = PROCESSED_LAYER_NAME in [layer.name for layer in font.layers]

        autohinted = False
        if is_binary:
            source = HintSource.AUTOHINT_V2
            if count_glyphs_with_source(font, source) == 0:
                font = _autohint_in_place(font, log)
                source = HintSource.PROCESSED_LAYER
                autohinted = True
        elif hint_source == "auto":
            source = detect_font_source(font)
            if source is None:
                font = _autohint_in_place(font, log)
                source = HintSource.PROCESSED_LAYER
                autohinted = True
        else:
            source = _resolve_source(font, hint_source, is_binary, log)
            if source is None:
                return ProcessResult(
                    success=False,
                    error=f"No hints found for hint_source={hint_source!r}",
                )

        glyphs_total = len(font)
        glyphs_with_hints = count_glyphs_with_source(font, source)
        log.info(
            f"pipeline: source={source.value} glyphs={glyphs_total} "
            f"with_hints={glyphs_with_hints} autohinted={autohinted}"
        )

        optimized_count = 0
        if optimize:
            if source != HintSource.PROCESSED_LAYER:
                imported = import_all_to_processed(font, source)
                log.info(f"pipeline: imported {imported} glyphs to processedglyphs")
            stats = optimize_font(font)
            optimized_count = stats["optimized"]
            log.info(f"pipeline: optimized={stats['optimized']} skipped={stats['skipped']}")
            export_all_from_processed(font, "v2")
        else:
            if source == HintSource.AUTOHINT_V2:
                pass
            elif source == HintSource.PROCESSED_LAYER:
                export_all_from_processed(font, "v2")
            elif source == HintSource.PUBLIC_PS:
                import_all_to_processed(font, source)
                export_all_from_processed(font, "v2")

        if not had_processed_layer:
            current = [layer.name for layer in font.layers]
            if PROCESSED_LAYER_NAME in current:
                font.removeLayer(PROCESSED_LAYER_NAME)
                log.info("pipeline: removed temporary processedglyphs layer")

        if output_ufo.exists():
            shutil.rmtree(output_ufo)
        font.save(str(output_ufo))
        log.info(f"pipeline: saved UFO to {output_ufo}")

        from ufo_tdkit_tools.compilation.compiler import (
            compile_otf_preserve_optimized,
        )

        ok = compile_otf_preserve_optimized(
            str(output_ufo),
            str(output_otf),
            logger=log,
        )
        if not ok:
            return ProcessResult(
                success=False,
                hint_source_used=source.value,
                glyphs_total=glyphs_total,
                glyphs_with_hints=glyphs_with_hints,
                optimized=optimize,
                optimized_count=optimized_count,
                autohinted=autohinted,
                error="OTF compilation failed",
            )

        log.info(f"pipeline: compiled OTF to {output_otf}")
        return ProcessResult(
            success=True,
            hint_source_used=source.value,
            glyphs_total=glyphs_total,
            glyphs_with_hints=glyphs_with_hints,
            optimized=optimize,
            optimized_count=optimized_count,
            autohinted=autohinted,
        )
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


def _autohint_in_place(font, log: logging.Logger):
    """Run ``afdko.otfautohint`` on the working UFO and return a reloaded font.

    The autohinter writes hints to the processedglyphs layer of the on-disk
    UFO. We drop the in-memory font, invoke ``hintFiles`` on the path, and
    reopen — the caller proceeds with ``HintSource.PROCESSED_LAYER``.
    """
    import fontParts.world as fp
    from afdko.otfautohint.autohint import ACOptions, hintFiles

    ufo_path = str(font.path)
    log.info(f"pipeline: running otfautohint on {ufo_path}")

    options = ACOptions()
    options.inputPaths = [ufo_path]
    options.outputPaths = [ufo_path]
    options.allowNoBlues = True
    hintFiles(options)

    return fp.OpenFont(ufo_path, showInterface=False)


def _resolve_source(font, hint_source: str, is_binary: bool, log: logging.Logger):
    """Resolve the ``hint_source`` argument into a concrete :class:`HintSource`."""
    from ufo_tdkit_tools.ps_hints.batch import (
        count_glyphs_with_source,
        detect_font_source,
    )
    from ufo_tdkit_tools.ps_hints.parser import HintSource

    if is_binary:
        return HintSource.AUTOHINT_V2

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
