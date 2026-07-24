# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
UFO to OTF compilation with PS hint preservation.

Hybrid strategy for preserving PostScript hints during UFO -> OTF compilation:
1. Compile with ufo2ft (correct metadata, no subroutinization, SOURCE glyph names)
2. Compile with tx + makeotf (hints from glyph.lib, no subroutinization)
3. Per-glyph charstring merge (hinted charstrings into shell CFF)
4. Transfer PrivateDict hint parameters
5. Rename to production names + subroutinize via ufo2ft's PostProcessor

Both compiles must share one glyph namespace for step 3 to match anything, so
the shell is built with ``useProductionNames=False`` and renaming is deferred
to step 5 -- see :func:`_finalize_shell`.

Extracted from TDKit (src/tdkit/compilers/otf_ttf.py).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import traceback

from fontTools.ttLib import TTFont

logger = logging.getLogger(__name__)


def is_preserve_mode(pshinter):
    """Check if pshinter value is one of the preserve modes.

    Args:
        pshinter: The pshinter configuration value

    Returns:
        True if pshinter is "preserve" or "preserve-optimized"
    """
    return isinstance(pshinter, str) and pshinter in ("preserve", "preserve-optimized")


def _compile_ufo2ft_shell(ufo_path, output_path, logger=None):
    """Compile UFO to OTF using ufo2ft (correct metadata tables, no PS hints).

    Glyphs keep their SOURCE names: ``useProductionNames=False``. The hinted
    donor built by ``tx -t1`` + ``makeotf`` ignores ``public.postscriptNames``
    and always uses source names, so renaming the shell here would leave the
    per-glyph merge with two disjoint namespaces and silently drop the hints of
    every renamed glyph. Production names are applied after the merge by
    :func:`_finalize_shell`, exactly as a plain ``compileOTF`` would.

    Handles feature compilation errors by retrying with empty features.

    Args:
        ufo_path: Path to UFO source
        output_path: Path for output OTF
        logger: Optional logger

    Returns:
        The :class:`defcon.Font` the shell was built from (truthy) on success,
        ``None`` on failure. The font is needed later to reproduce ufo2ft's
        production-name mapping.
    """
    from defcon import Font
    from ufo2ft import compileOTF

    try:
        ufo = Font(ufo_path)
        try:
            # optimizeCFF=1: specialize charstrings but skip subroutinization.
            # The shell's charstrings will be replaced by hinted versions from makeotf,
            # so subroutinization here would only create dangling subrs in the CFF table.
            otf = compileOTF(ufo, optimizeCFF=1, useProductionNames=False)
        except Exception as e:
            # LAST-RESORT fallback: produce *some* OTF rather than fail the build.
            # This DROPS the entire feature set (GSUB + explicit GPOS features),
            # so it must never pass silently — the usual trigger is an unresolved
            # feature include() after the UFO was relocated to a temp dir. Escalate
            # loudly so the layout loss is visible in the log, not discovered later
            # in the shipped binary.
            if logger:
                logger.error(
                    f"Feature compilation FAILED ({e}); recompiling WITHOUT features "
                    f"as a last resort — the resulting OTF will have NO GSUB and only "
                    f"auto-generated GPOS. Check for unresolved include() paths in "
                    f"{ufo_path}."
                )
            ufo = Font(ufo_path)
            ufo.features.text = ""
            otf = compileOTF(ufo, optimizeCFF=1, useProductionNames=False)
        otf.save(output_path)
        return ufo
    except Exception as e:
        if logger:
            logger.error(f"ufo2ft compilation failed: {e}")
        traceback.print_exc()
        return None


def _compile_makeotf_hinted(
    ufo_path, output_path, makeotf_args, logger=None, tx_path=None, makeotf_path=None
):
    """Compile UFO to OTF using tx + makeotf subprocesses.

    Pipeline: tx -t1 (UFO -> Type1) -> makeotf (Type1 -> OTF)

    Uses the ``makeotf`` wrapper (not the deprecated ``makeotfexe`` binary,
    which is now a stub on AFDKO releases that schedule its removal after
    March 2027). ``makeotf`` is invoked as a subprocess, not via its Python
    API, so there is no shared global state and parallel
    ``ProcessPoolExecutor`` workers stay isolated.

    Args:
        ufo_path: Path to UFO source
        output_path: Path for output OTF
        makeotf_args: List of makeotf CLI arguments (ignored -- rebuilt internally)
        logger: Optional logger
        tx_path: Absolute path to tx binary (resolved in main process)
        makeotf_path: Absolute path to makeotf binary (resolved in main process)

    Returns:
        True on success, False on failure
    """
    import subprocess as _sp
    import sys

    # Resolve tool paths with triple fallback:
    # 1. Pre-resolved paths from main process (via config)
    # 2. shutil.which() (works if PATH includes virtualenv bin/)
    # 3. Same directory as sys.executable (always works in virtualenv)
    venv_bin = os.path.dirname(sys.executable)

    tx_bin = tx_path or shutil.which("tx") or os.path.join(venv_bin, "tx")
    makeotf_bin = (
        makeotf_path
        or shutil.which("makeotf")
        or os.path.join(venv_bin, "makeotf")
    )

    if not os.path.isfile(tx_bin) or not os.path.isfile(makeotf_bin):
        if logger:
            logger.error(
                f"Tool resolution failed: tx={tx_bin} (exists={os.path.isfile(tx_bin)}), "
                f"makeotf={makeotf_bin} (exists={os.path.isfile(makeotf_bin)}), "
                f"venv_bin={venv_bin}, PATH={os.environ.get('PATH', '')[:200]}"
            )
        return False

    # makeotf is a Python wrapper that shells out to tx, addfeatures and spot;
    # it resolves them via PATH, so ensure the venv bin dir is on PATH.
    env = dict(os.environ)
    bin_dir = os.path.dirname(makeotf_bin)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

    # Extract -nS flag from args if present
    extra_flags = []
    for arg in makeotf_args:
        if arg in ("-nS", "-shw"):
            extra_flags.append(arg)

    t1_path = output_path.replace(".otf", ".ps")
    try:
        # Step 1: Convert UFO to Type 1 using tx
        tx_result = _sp.run(
            [tx_bin, "-t1", ufo_path, t1_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if tx_result.returncode != 0:
            if logger:
                logger.error(f"tx -t1 failed (exit {tx_result.returncode})")
                if tx_result.stderr.strip():
                    logger.error(f"tx stderr: {tx_result.stderr.strip()}")
            return False

        # Step 2: Compile with makeotf wrapper. The donor OTF exists ONLY as a
        # source of hinted charstrings and PrivateDict hint parameters (see the
        # per-glyph merge in compile_otf_preserve_optimized) — its GSUB/GPOS
        # are discarded, so it is compiled WITHOUT any features file. Do NOT
        # pass -ff here: on some platforms makeotfexe aborts with
        # std::system_error whenever -ff is present (even for an empty file),
        # which silently downgrades preserve mode to unhinted shell output.
        # Wrapper feature auto-discovery is not a concern either: makeotf
        # searches next to the INPUT font, and the tx-emitted .ps sits alone
        # in a temp directory. Output features come from the ufo2ft shell.
        cmd = [makeotf_bin, "-f", t1_path, "-o", output_path]
        cmd += extra_flags
        mko_result = _sp.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        if mko_result.returncode != 0:
            if logger:
                logger.error(f"makeotf failed (exit {mko_result.returncode})")
                if mko_result.stdout.strip():
                    logger.error(f"makeotf stdout: {mko_result.stdout.strip()}")
                if mko_result.stderr.strip():
                    logger.error(f"makeotf stderr: {mko_result.stderr.strip()}")
            return False
        # Defensive: makeotf has historically returned 0 even when no output
        # file was produced (e.g. the makeotfexe stub before its removal).
        if not os.path.exists(output_path):
            if logger:
                logger.error(
                    "makeotf returned 0 but produced no output file; "
                    f"stdout: {mko_result.stdout.strip()[:500]}; "
                    f"stderr: {mko_result.stderr.strip()[:500]}"
                )
            return False
        if logger and mko_result.stdout.strip():
            for line in mko_result.stdout.strip().split("\n"):
                logger.info(f"makeotf: {line}")
    except FileNotFoundError as e:
        if logger:
            logger.error(f"Tool not found: {e}")
        return False
    except _sp.TimeoutExpired:
        if logger:
            logger.error("makeotf compilation timed out (180s)")
        return False
    except Exception as e:
        if logger:
            logger.error(f"makeotf compilation error: {e}")
            logger.error(traceback.format_exc())
        else:
            traceback.print_exc()
        return False
    finally:
        # Clean up temporary Type 1 file
        if os.path.exists(t1_path):
            os.remove(t1_path)

    return True


def generate_goadb(ufo_path, glyph_order, output_path, logger=None):
    """Generate GlyphOrderAndAliasDB file matching a specific glyph order.

    Creates a GOADB with identical source and production names (no renaming),
    used to force makeotf to match ufo2ft's glyph order.

    Args:
        ufo_path: Path to UFO source (for unicode lookups)
        glyph_order: List of glyph names in desired order
        output_path: Where to write the GOADB file
        logger: Optional logger

    Returns:
        Path to generated GOADB file
    """
    from defcon import Font

    ufo = Font(ufo_path)
    glyph_unicodes = {}
    for glyph_name in ufo.keys():
        glyph = ufo[glyph_name]
        if glyph.unicodes:
            glyph_unicodes[glyph_name] = glyph.unicodes[0]

    lines = []
    for glyph_name in glyph_order:
        unicode_val = glyph_unicodes.get(glyph_name)
        if unicode_val is not None:
            uni_str = f"uni{unicode_val:04X}"
            lines.append(f"{glyph_name}\t{glyph_name}\t{uni_str}")
        else:
            lines.append(f"{glyph_name}\t{glyph_name}")

    with open(output_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    if logger:
        logger.info(f"Generated GOADB with {len(lines)} entries: {output_path}")
    return output_path


# ── Processedglyphs layer preparation ────────────────────────────────────────


def _decompose_glyph(glyph, source_layer, logger=None):
    """Decompose all components in a glyph into outlines."""
    try:
        for component in list(glyph.components):
            base_name = component.baseGlyph
            if base_name not in source_layer:
                if logger:
                    logger.warning(
                        f"Component base '{base_name}' not found for '{glyph.name}'"
                    )
                continue
            base_glyph = source_layer[base_name]
            xScale, xyScale, yxScale, yScale, xOffset, yOffset = component.transformation
            from fontTools.pens.transformPen import TransformPointPen

            tpen = TransformPointPen(
                glyph.getPointPen(),
                (xScale, xyScale, yxScale, yScale, xOffset, yOffset),
            )
            base_glyph.drawPoints(tpen)
        glyph.clearComponents()
    except Exception as e:
        if logger:
            logger.warning(f"Decomposition failed for '{glyph.name}': {e}")


def _ensure_hint_ref_point(glyph):
    """Ensure the first on-curve point has a name (for makeotf hint referencing)."""
    for contour in glyph:
        for point in contour:
            if point.segmentType is not None:  # on-curve point
                if not point.name:
                    point.name = "hintRef0000"
                return


def _deep_copy_hint_data(hint_data):
    """Deep-copy hint data (dict/list structure)."""
    return json.loads(json.dumps(hint_data))


def prepare_processedglyphs(ufo_path, logger=None):
    """Rebuild processedglyphs layer from default layer hints.

    Creates (or clears) the ``com.adobe.type.processedglyphs`` layer and
    populates it from default-layer glyphs that carry
    ``com.adobe.type.autohint.v2`` hint data.

    Args:
        ufo_path: Path to a UFO directory
        logger: Optional logger

    Returns:
        Number of glyphs prepared (0 means no hints were found)
    """
    import defcon

    try:
        from afdko.otfautohint.ufoFont import HashPointPen
    except ImportError:
        if logger:
            logger.error("afdko not installed, cannot prepare processedglyphs")
        return 0

    try:
        from booleanOperations.booleanGlyph import BooleanGlyph
    except ImportError:
        BooleanGlyph = None
        if logger:
            logger.warning("booleanOperations not available, overlap removal skipped")

    from ufo_tdkit_tools.constants import ADOBE_HINT_KEY_V2, PROCESSED_LAYER_NAME

    HASHMAP_NAME = "com.adobe.type.processedHashMap"

    ufo = defcon.Font(ufo_path)
    default_layer = ufo.layers.defaultLayer

    # Create or clear the processed layer
    if PROCESSED_LAYER_NAME in ufo.layers:
        processed = ufo.layers[PROCESSED_LAYER_NAME]
        for gname in list(processed.keys()):
            del processed[gname]
    else:
        processed = ufo.layers.newLayer(PROCESSED_LAYER_NAME)

    hashmap = {"hashMapVersion": (1, 0)}
    prepared = 0

    for glyph_name in default_layer.keys():
        glyph = default_layer[glyph_name]

        # Skip empty glyphs (no contours and no components, e.g. space)
        if len(glyph) == 0 and len(glyph.components) == 0:
            continue

        hint_data = glyph.lib.get(ADOBE_HINT_KEY_V2)
        if hint_data is None:
            continue

        # -- 1. Compute hash of default-layer outline (source hash) --------
        hp = HashPointPen(glyph, glyphset=default_layer)
        glyph.drawPoints(hp)
        src_hash = hp.getHash()

        # -- 2. Copy glyph into processedglyphs layer ----------------------
        processed.newGlyph(glyph_name)
        pg = processed[glyph_name]
        glyph.drawPoints(pg.getPointPen())
        pg.width = glyph.width

        # -- 3. Decompose components ----------------------------------------
        if pg.components:
            _decompose_glyph(pg, default_layer, logger)

        # -- 4. Remove overlaps (only if multiple contours) -----------------
        if len(pg) > 1 and BooleanGlyph is not None:
            try:
                bg = BooleanGlyph(pg)
                result = bg.removeOverlap()
                pg.clearContours()
                result.draw(pg.getPen())
            except Exception as e:
                if logger:
                    logger.warning(f"Overlap removal failed for '{glyph_name}': {e}")

        # -- 5. Ensure at least one named on-curve point (hintRef) ----------
        _ensure_hint_ref_point(pg)

        # -- 6. Transfer hint data and compute processed hash (id) ----------
        proc_hp = HashPointPen(pg)
        pg.drawPoints(proc_hp)
        proc_hash = proc_hp.getHash()

        # Clone hint data and set the 'id' to the processed glyph hash
        hint_copy = _deep_copy_hint_data(hint_data)
        if isinstance(hint_copy, dict):
            hint_copy["id"] = proc_hash

        pg.lib[ADOBE_HINT_KEY_V2] = hint_copy

        # -- 7. Add to hashmap ---------------------------------------------
        hashmap[glyph_name] = [src_hash, ["autohint"]]
        prepared += 1

    if prepared == 0:
        # Nothing to write -- don't save a useless layer
        if PROCESSED_LAYER_NAME in ufo.layers:
            del ufo.layers[PROCESSED_LAYER_NAME]
        if logger:
            logger.info("prepare_processedglyphs: no hinted glyphs found")
        return 0

    # -- 8. Write processedHashMap ------------------------------------------
    lines = ["{"]
    for key in sorted(hashmap.keys()):
        lines.append(f"'{key}': {hashmap[key]!r},")
    lines.append("}")
    lines.append("")
    hashmap_bytes = "\n".join(lines).encode("utf-8")

    # defcon's data directory API
    ufo.data[HASHMAP_NAME] = hashmap_bytes

    ufo.save()

    if logger:
        logger.info(f"prepare_processedglyphs: prepared {prepared} glyphs in {ufo_path}")
    return prepared


# ── Preserve compilation ────────────────────────────────────────────────────


# Charstring operators that carry PS hints.
HINT_OPS = frozenset({"hstem", "vstem", "hstemhm", "vstemhm", "hintmask", "cntrmask"})

# Stack-clearing operators that carry a leading width operand exactly when
# their argument count is odd. (The stem/mask operators take pairs of
# coordinates, so an odd count means one extra leading value: the width.)
_WIDTH_ODD_ARG_OPS = HINT_OPS

# Warn when the two compiles agree on less than this share of the shell's glyphs.
_NAME_COVERAGE_WARN_RATIO = 0.95


def _split_width_prefix(program):
    """Split a Type 2 charstring program into ``(width_prefix, body)``.

    A charstring may start with a single width operand, encoded as
    ``width - Private.nominalWidthX`` and present only when the glyph's width
    differs from ``Private.defaultWidthX``. Its presence is deduced from the
    argument count of the first stack-clearing operator (Type 2 spec, "Width").

    The donor and the shell have their own Private dicts, so a donor program
    must never keep its own width prefix when moved into the shell -- the same
    operand would decode against a different ``nominalWidthX``. Callers splice
    the shell's own prefix onto the donor's body instead.

    Args:
        program: Decompiled charstring program (list of numbers and operators).

    Returns:
        ``(width_prefix, body)`` where ``width_prefix`` is a one-element list
        or ``[]``, and their concatenation is the original program.
    """
    nargs = 0
    for token in program:
        if not isinstance(token, str):
            nargs += 1
            continue
        if token in _WIDTH_ODD_ARG_OPS:
            has_width = nargs % 2 == 1
        elif token == "rmoveto":
            has_width = nargs > 2
        elif token in ("hmoveto", "vmoveto"):
            has_width = nargs > 1
        elif token == "endchar":
            # 0 args: plain; 4: seac-like; 1 / 5: the extra leading arg is the width.
            has_width = nargs in (1, 5)
        else:
            # Not a stack-clearing operator that can carry a width (e.g. a
            # charstring opening with 'callsubr'): leave the program alone.
            has_width = False
        if has_width:
            return list(program[:1]), list(program[1:])
        return [], list(program)
    return [], list(program)


def _finalize_shell(shell, ufo, otf_path, subroutinize, logger=None):
    """Apply production glyph names (and optionally subroutinize), then save.

    The shell was compiled with ``useProductionNames=False`` so that the
    per-glyph merge could match the donor's source names. Renaming is done here
    through ufo2ft's own :class:`~ufo2ft.postProcessor.PostProcessor`, which is
    the very code a plain ``compileOTF`` would have run: it honours
    ``public.postscriptNames``, derives ``uniXXXX`` names when the lib key is
    absent but renaming is requested, uniquifies collisions and strips invalid
    characters. Reimplementing any of that here would drift from ufo2ft.

    Args:
        shell: :class:`TTFont` holding the merged CFF.
        ufo: :class:`defcon.Font` the shell was compiled from.
        otf_path: Destination path.
        subroutinize: Run cffsubr on the CFF table.
        logger: Optional logger.

    Returns:
        True if the output file exists afterwards.
    """
    try:
        from ufo2ft.postProcessor import PostProcessor

        # useProductionNames=None: reproduce compileOTF's default decision from
        # the UFO lib (public.postscriptNames / useProductionNames / keepGlyphNames).
        shell = PostProcessor(shell, ufo).process(
            useProductionNames=None, optimizeCFF=subroutinize
        )
    except Exception as e:
        # Never lose the font over a post-processing hiccup: fall back to the
        # documented lib mapping plus a direct cffsubr call.
        if logger:
            logger.warning(
                f"ufo2ft PostProcessor unavailable or failed ({e}); falling back to "
                "public.postscriptNames renaming"
            )
        _rename_glyphs_fallback(shell, ufo, logger)
        if subroutinize:
            import cffsubr

            cffsubr.subroutinize(shell)

    shell.save(otf_path)
    shell.close()
    return os.path.exists(otf_path)


def _rename_glyphs_fallback(shell, ufo, logger=None):
    """Rename shell glyphs from ``public.postscriptNames`` (PostProcessor fallback)."""
    rename_map = ufo.lib.get("public.postscriptNames")
    if not rename_map:
        return
    rename_map = {src: dst for src, dst in rename_map.items() if src != dst}
    if not rename_map:
        return
    shell.setGlyphOrder([rename_map.get(n, n) for n in shell.getGlyphOrder()])
    cff = shell["CFF "].cff.topDictIndex[0]
    cff.CharStrings.charStrings = {
        rename_map.get(n, n): v for n, v in cff.CharStrings.charStrings.items()
    }
    cff.charset = [rename_map.get(n, n) for n in cff.charset]
    if logger:
        logger.info(f"Renamed {len(rename_map)} glyphs to production names (fallback)")


def compile_otf_preserve(
    ufo_path, otf_path, logger=None, pshash_rebuild=False,
    tx_path=None, makeotf_path=None
):
    """Compile OTF preserving PS hints from UFO.

    Delegates to compile_otf_preserve_optimized which uses the per-glyph
    charstring merge approach.

    Args:
        ufo_path: Path to UFO source with com.adobe.type.autohint.v2 hints
        otf_path: Path for output OTF
        logger: Optional logger
        pshash_rebuild: If True, rebuild processedglyphs layer before compilation
        tx_path: Absolute path to tx binary (for parallel workers)
        makeotf_path: Absolute path to makeotf binary (for parallel workers)

    Returns:
        True on success, False on failure
    """
    return compile_otf_preserve_optimized(
        ufo_path,
        otf_path,
        logger,
        pshash_rebuild=pshash_rebuild,
        tx_path=tx_path,
        makeotf_path=makeotf_path,
    )


def compile_otf_preserve_optimized(
    ufo_path, otf_path, logger=None, pshash_rebuild=False,
    tx_path=None, makeotf_path=None, stats=None
):
    """Compile OTF preserving PS hints using per-glyph charstring merge.

    Hybrid strategy:
    1. Compile with ufo2ft (optimizeCFF=1, source glyph names) -> shell OTF
    2. Compile with tx -t1 + makeotf -nS -> hinted OTF (hints from glyph.lib)
    3. Copy individual hinted charstrings per-glyph into shell's CFF
    4. Copy PrivateDict hint parameters (BlueValues, StemSnaps, etc.)
    5. Apply production glyph names + cffsubr subroutinization (~38% size
       reduction, hints preserved in subrs)

    Both compiles keep the UFO's source glyph names, so the merge in step 3
    matches every glyph; production names are applied only in step 5.

    Args:
        ufo_path: Path to UFO source with com.adobe.type.autohint.v2 hints
        otf_path: Path for output OTF
        logger: Optional logger
        pshash_rebuild: If True, rebuild processedglyphs layer before makeotf
        tx_path: Absolute path to tx binary (for parallel workers)
        makeotf_path: Absolute path to makeotf binary (for parallel workers)
        stats: Optional mutable dict, filled with
               {'hints_transferred': int, 'total_glyphs': int,
                'donor_hinted': int, 'name_mismatch': int}

    Returns:
        True on success, False on failure
    """
    PRIVATE_HINT_ATTRS = [
        "BlueValues", "OtherBlues", "FamilyBlues", "FamilyOtherBlues",
        "BlueScale", "BlueShift", "BlueFuzz",
        "StdHW", "StdVW", "StemSnapH", "StemSnapV", "ForceBold",
    ]

    temp_dir = tempfile.mkdtemp(prefix="tdkit_preserve_opt_")
    shell_path = os.path.join(temp_dir, "shell.otf")
    hinted_path = os.path.join(temp_dir, "hinted.otf")

    try:
        # Step 1: Compile ufo2ft shell
        if logger:
            logger.info("Preserve-optimized: compiling ufo2ft shell")
        shell_ufo = _compile_ufo2ft_shell(ufo_path, shell_path, logger)
        if shell_ufo is None:
            return False

        # Step 1.5: Validate hint data
        from ufo_tdkit_tools.ps_hints.validator import validate_ps_hints

        hint_report = validate_ps_hints(ufo_path, logger)
        if not hint_report["valid"]:
            if logger:
                logger.warning(
                    f"PS hint validation found {len(hint_report['errors'])} error(s) -- "
                    "compilation will continue but some hints may be lost"
                )
                for err in hint_report["errors"][:10]:
                    logger.error(f"  Hint error in '{err['glyph']}': {err['message']}")
                if len(hint_report["errors"]) > 10:
                    logger.error(
                        f"  ... and {len(hint_report['errors']) - 10} more error(s)"
                    )
        if hint_report["warnings"] and logger:
            for warn in hint_report["warnings"][:5]:
                logger.warning(f"  Hint warning in '{warn['glyph']}': {warn['message']}")
            if len(hint_report["warnings"]) > 5:
                logger.warning(
                    f"  ... and {len(hint_report['warnings']) - 5} more warning(s)"
                )

        # Note: prepare_processedglyphs is NOT called here. tx -t1 reads hints
        # directly from com.adobe.type.autohint.v2 in the default layer's glyph.lib.
        # Creating a processedglyphs layer would cause tx to read decomposed/overlap-removed
        # glyphs from that layer, where the hint point references no longer match the
        # modified outlines, resulting in lost hints.

        # Step 2: Compile makeotf without subroutinization
        if logger:
            logger.info("Preserve-optimized: compiling makeotf (no subroutinization)")
        makeotf_args = ["-nS", "-f", ufo_path, "-o", hinted_path]
        if not _compile_makeotf_hinted(
            ufo_path, hinted_path, makeotf_args, logger,
            tx_path=tx_path, makeotf_path=makeotf_path,
        ):
            if logger:
                logger.error("makeotf compilation failed in preserve-optimized mode")
            return False

        # Step 3: Per-glyph charstring merge
        shell = TTFont(shell_path)
        hinted = TTFont(hinted_path)

        shell_td = shell["CFF "].cff.topDictIndex[0]
        hinted_td = hinted["CFF "].cff.topDictIndex[0]
        shell_cs = shell_td.CharStrings
        hinted_cs = hinted_td.CharStrings

        shell_names = list(shell_cs.keys())
        missing = [name for name in shell_names if name not in hinted_cs]
        if missing and shell_names and logger:
            # A large gap means the two compiles disagree on glyph names, which
            # costs every unmatched glyph its hints without failing the build.
            coverage = 1.0 - len(missing) / len(shell_names)
            log_at = logger.warning if coverage < _NAME_COVERAGE_WARN_RATIO else logger.info
            log_at(
                f"Preserve-optimized: {len(missing)}/{len(shell_names)} shell glyphs "
                f"have no counterpart in the makeotf donor "
                f"({coverage:.1%} name coverage); their hints cannot be merged. "
                f"Sample: {missing[:10]}"
            )

        transferred = 0
        donor_hinted = 0
        for glyph_name in shell_names:
            if glyph_name not in hinted_cs:
                continue
            hinted_charstring = hinted_cs[glyph_name]
            hinted_charstring.decompile()
            has_hints = any(
                isinstance(op, str) and op in HINT_OPS
                for op in hinted_charstring.program
            )
            if has_hints:
                donor_hinted += 1
                shell_charstring = shell_cs[glyph_name]
                shell_charstring.decompile()
                # Keep the SHELL's width operand: the donor encodes its width
                # against its own Private.nominalWidthX / defaultWidthX, which
                # the merge does not carry over, so reusing the donor's prefix
                # would decode to a bogus advance width in the output CFF.
                shell_width, _ = _split_width_prefix(shell_charstring.program)
                _, hinted_body = _split_width_prefix(hinted_charstring.program)
                shell_charstring.program = shell_width + hinted_body
                transferred += 1

        # Step 4: Transfer PrivateDict hint parameters
        shell_private = shell_td.Private
        hinted_private = hinted_td.Private
        for attr in PRIVATE_HINT_ATTRS:
            hinted_val = getattr(hinted_private, attr, None)
            if hinted_val is not None:
                setattr(shell_private, attr, hinted_val)

        # Hinted donor glyphs the shell does not know about — the mirror image
        # of the name-coverage warning above, and the symptom of a namespace
        # mismatch that would otherwise pass as "this font has few hints".
        dropped = 0
        for glyph_name in hinted_cs.keys():
            if glyph_name in shell_cs:
                continue
            charstring = hinted_cs[glyph_name]
            charstring.decompile()
            if any(
                isinstance(op, str) and op in HINT_OPS for op in charstring.program
            ):
                dropped += 1
        if dropped and logger:
            logger.warning(
                f"Preserve-optimized: {dropped} hinted donor glyph(s) have no shell "
                "counterpart and were dropped"
            )

        hinted.close()

        if stats is not None:
            stats["hints_transferred"] = transferred
            stats["total_glyphs"] = len(shell_names)
            stats["donor_hinted"] = donor_hinted + dropped
            stats["name_mismatch"] = len(missing)

        if logger:
            logger.info(
                f"Preserve-optimized: transferred {transferred} hinted charstrings "
                f"({transferred}/{len(shell_names)} glyphs)"
            )

        if transferred == 0:
            if logger:
                logger.warning(
                    "No hinted glyphs found in UFO -- preserve mode has nothing to preserve. "
                    "Using standard ufo2ft compilation instead."
                )
            # Shell OTF is already a valid unhinted font -- it still needs its
            # production glyph names, which were deferred for the merge.
            return _finalize_shell(
                shell, shell_ufo, otf_path, subroutinize=False, logger=logger
            )

        # Step 5: Production glyph names + cffsubr subroutinization for ~38%
        # smaller CFF. Hints are preserved inside subroutines (callsubr/callgsubr).
        if logger:
            logger.info(
                "Preserve-optimized: applying production names and subroutinization"
            )
        saved = _finalize_shell(
            shell, shell_ufo, otf_path, subroutinize=True, logger=logger
        )

        if logger and saved:
            logger.info(f"Preserve-optimized: saved hybrid OTF: {otf_path}")
        return saved

    except Exception as e:
        if logger:
            logger.error(f"Preserve-optimized mode failed: {e}")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
