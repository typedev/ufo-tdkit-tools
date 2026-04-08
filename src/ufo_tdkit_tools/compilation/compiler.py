# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
UFO to OTF compilation with PS hint preservation.

Hybrid strategy for preserving PostScript hints during UFO -> OTF compilation:
1. Compile with ufo2ft (correct metadata, no subroutinization)
2. Compile with tx + makeotfexe (hints from glyph.lib, no subroutinization)
3. Per-glyph charstring merge (hinted charstrings into shell CFF)
4. Transfer PrivateDict hint parameters
5. Apply cffsubr subroutinization (~38% size reduction, hints in subrs)

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

    Handles feature compilation errors by retrying with empty features.

    Args:
        ufo_path: Path to UFO source
        output_path: Path for output OTF
        logger: Optional logger

    Returns:
        True on success, False on failure
    """
    from defcon import Font
    from ufo2ft import compileOTF

    try:
        ufo = Font(ufo_path)
        try:
            # optimizeCFF=1: specialize charstrings but skip subroutinization.
            # The shell's charstrings will be replaced by hinted versions from makeotf,
            # so subroutinization here would only create dangling subrs in the CFF table.
            otf = compileOTF(ufo, optimizeCFF=1)
        except Exception as e:
            if logger:
                logger.warning(f"Feature compilation failed ({e}), retrying without features")
            ufo = Font(ufo_path)
            ufo.features.text = ""
            otf = compileOTF(ufo, optimizeCFF=1)
        otf.save(output_path)
        return True
    except Exception as e:
        if logger:
            logger.error(f"ufo2ft compilation failed: {e}")
        traceback.print_exc()
        return False


def _compile_makeotf_hinted(
    ufo_path, output_path, makeotf_args, logger=None, tx_path=None, makeotfexe_path=None
):
    """Compile UFO to OTF using tx + makeotfexe subprocesses.

    Bypasses the Python makeotf wrapper (getOptions/runMakeOTF) which uses
    global state and internal subprocess calls that cause race conditions
    in parallel ProcessPoolExecutor workers.

    Pipeline: tx -t1 (UFO -> Type1) -> makeotfexe (Type1 -> OTF)

    Args:
        ufo_path: Path to UFO source
        output_path: Path for output OTF
        makeotf_args: List of makeotf CLI arguments (ignored -- rebuilt internally)
        logger: Optional logger
        tx_path: Absolute path to tx binary (resolved in main process)
        makeotfexe_path: Absolute path to makeotfexe binary (resolved in main process)

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
    makeotfexe_bin = (
        makeotfexe_path
        or shutil.which("makeotfexe")
        or os.path.join(venv_bin, "makeotfexe")
    )

    if not os.path.isfile(tx_bin) or not os.path.isfile(makeotfexe_bin):
        if logger:
            logger.error(
                f"Tool resolution failed: tx={tx_bin} (exists={os.path.isfile(tx_bin)}), "
                f"makeotfexe={makeotfexe_bin} (exists={os.path.isfile(makeotfexe_bin)}), "
                f"venv_bin={venv_bin}, PATH={os.environ.get('PATH', '')[:200]}"
            )
        return False

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

        # Step 2: Compile with makeotfexe
        cmd = [makeotfexe_bin, "-f", t1_path, "-o", output_path] + extra_flags
        mko_result = _sp.run(cmd, capture_output=True, text=True, timeout=120)
        if mko_result.returncode != 0:
            if logger:
                logger.error(f"makeotfexe failed (exit {mko_result.returncode})")
                if mko_result.stdout.strip():
                    logger.error(f"makeotfexe stdout: {mko_result.stdout.strip()}")
                if mko_result.stderr.strip():
                    logger.error(f"makeotfexe stderr: {mko_result.stderr.strip()}")
            return False
        if logger and mko_result.stdout.strip():
            for line in mko_result.stdout.strip().split("\n"):
                logger.info(f"makeotfexe: {line}")
    except FileNotFoundError as e:
        if logger:
            logger.error(f"Tool not found: {e}")
        return False
    except _sp.TimeoutExpired:
        if logger:
            logger.error("makeotf compilation timed out (120s)")
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

    return os.path.exists(output_path)


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


def compile_otf_preserve(
    ufo_path, otf_path, logger=None, pshash_rebuild=False,
    tx_path=None, makeotfexe_path=None
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
        makeotfexe_path: Absolute path to makeotfexe binary (for parallel workers)

    Returns:
        True on success, False on failure
    """
    return compile_otf_preserve_optimized(
        ufo_path,
        otf_path,
        logger,
        pshash_rebuild=pshash_rebuild,
        tx_path=tx_path,
        makeotfexe_path=makeotfexe_path,
    )


def compile_otf_preserve_optimized(
    ufo_path, otf_path, logger=None, pshash_rebuild=False,
    tx_path=None, makeotfexe_path=None, stats=None
):
    """Compile OTF preserving PS hints using per-glyph charstring merge.

    Hybrid strategy:
    1. Compile with ufo2ft (optimizeCFF=1) -> shell OTF (correct metadata, no subrs)
    2. Compile with tx -t1 + makeotfexe -nS -> hinted OTF (hints from glyph.lib)
    3. Copy individual hinted charstrings per-glyph into shell's CFF
    4. Copy PrivateDict hint parameters (BlueValues, StemSnaps, etc.)
    5. Apply cffsubr subroutinization (~38% size reduction, hints preserved in subrs)

    Args:
        ufo_path: Path to UFO source with com.adobe.type.autohint.v2 hints
        otf_path: Path for output OTF
        logger: Optional logger
        pshash_rebuild: If True, rebuild processedglyphs layer before makeotf
        tx_path: Absolute path to tx binary (for parallel workers)
        makeotfexe_path: Absolute path to makeotfexe binary (for parallel workers)
        stats: Optional mutable dict, filled with
               {'hints_transferred': int, 'total_glyphs': int}

    Returns:
        True on success, False on failure
    """
    HINT_OPS = frozenset(
        {"hstem", "vstem", "hstemhm", "vstemhm", "hintmask", "cntrmask"}
    )
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
        if not _compile_ufo2ft_shell(ufo_path, shell_path, logger):
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
            tx_path=tx_path, makeotfexe_path=makeotfexe_path,
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

        transferred = 0
        for glyph_name in shell_cs.keys():
            if glyph_name not in hinted_cs:
                continue
            hinted_charstring = hinted_cs[glyph_name]
            hinted_charstring.decompile()
            has_hints = any(
                isinstance(op, str) and op in HINT_OPS
                for op in hinted_charstring.program
            )
            if has_hints:
                shell_charstring = shell_cs[glyph_name]
                shell_charstring.decompile()
                shell_charstring.program = hinted_charstring.program
                transferred += 1

        # Step 4: Transfer PrivateDict hint parameters
        shell_private = shell_td.Private
        hinted_private = hinted_td.Private
        for attr in PRIVATE_HINT_ATTRS:
            hinted_val = getattr(hinted_private, attr, None)
            if hinted_val is not None:
                setattr(shell_private, attr, hinted_val)

        hinted.close()

        if stats is not None:
            stats["hints_transferred"] = transferred
            stats["total_glyphs"] = len(shell_cs.keys())

        if logger:
            logger.info(
                f"Preserve-optimized: transferred {transferred} hinted charstrings"
            )

        if transferred == 0:
            if logger:
                logger.warning(
                    "No hinted glyphs found in UFO -- preserve mode has nothing to preserve. "
                    "Using standard ufo2ft compilation instead."
                )
            # Shell OTF is already a valid unhinted font -- just save it directly
            shell.save(otf_path)
            shell.close()
            return os.path.exists(otf_path)

        # Step 5: Subroutinize with cffsubr for ~38% smaller CFF
        # Hints are preserved inside subroutines (callsubr/callgsubr).
        import cffsubr

        if logger:
            logger.info("Preserve-optimized: applying cffsubr subroutinization")
        cffsubr.subroutinize(shell)

        shell.save(otf_path)
        shell.close()

        if logger:
            logger.info(f"Preserve-optimized: saved hybrid OTF: {otf_path}")
        return os.path.exists(otf_path)

    except Exception as e:
        if logger:
            logger.error(f"Preserve-optimized mode failed: {e}")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
