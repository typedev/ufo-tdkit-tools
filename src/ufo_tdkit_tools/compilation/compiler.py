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


class FeatureCompilationError(RuntimeError):
    """The UFO's feature code does not compile; no OTF is written.

    There is deliberately no "compile without features" fallback: it would
    ship an OTF with no GSUB/GDEF and only the auto-generated kerning in GPOS,
    and nothing about the file says so. Callers must let this propagate (or
    report the face as failed), never retry around it.
    """


def _is_feature_error(exc):
    """True if ``exc`` (or anything in its cause/context chain) is a feaLib error."""
    from fontTools.feaLib.error import FeatureLibError

    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, FeatureLibError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def _compile_ufo2ft_shell(ufo_path, output_path, logger=None):
    """Compile UFO to OTF using ufo2ft (correct metadata tables, no PS hints).

    Glyphs keep their SOURCE names: ``useProductionNames=False``. The hinted
    donor built by ``tx -t1`` + ``makeotf`` ignores ``public.postscriptNames``
    and always uses source names, so renaming the shell here would leave the
    per-glyph merge with two disjoint namespaces and silently drop the hints of
    every renamed glyph. Production names are applied after the merge by
    :func:`_finalize_shell`, exactly as a plain ``compileOTF`` would.

    Args:
        ufo_path: Path to UFO source
        output_path: Path for output OTF
        logger: Optional logger

    Returns:
        The :class:`defcon.Font` the shell was built from (truthy) on success,
        ``None`` on failure. The font is needed later to reproduce ufo2ft's
        production-name mapping.

    Raises:
        FeatureCompilationError: the feature code does not compile (unknown
            glyph class, missing glyph, unresolved ``include()``, syntax error).
    """
    from defcon import Font
    from ufo2ft import compileOTF

    try:
        ufo = Font(ufo_path)
        # optimizeCFF=1: specialize charstrings but skip subroutinization.
        # The shell's charstrings will be replaced by hinted versions from makeotf,
        # so subroutinization here would only create dangling subrs in the CFF table.
        otf = compileOTF(ufo, optimizeCFF=1, useProductionNames=False)
        otf.save(output_path)
        return ufo
    except Exception as e:
        if _is_feature_error(e):
            # No fallback: compiling without features would ship an OTF with
            # no GSUB/GDEF and only auto-generated kerning, silently.
            raise FeatureCompilationError(str(e)) from e
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
    # Step 3 goes through shutil.which too, so Windows' PATHEXT finds tx.exe /
    # makeotf.exe in the venv's Scripts directory.
    venv_bin = os.path.dirname(sys.executable)

    def _resolve(explicit, name):
        return (
            explicit
            or shutil.which(name)
            or shutil.which(name, path=venv_bin)
            or os.path.join(venv_bin, name)
        )

    tx_bin = _resolve(tx_path, "tx")
    makeotf_bin = _resolve(makeotf_path, "makeotf")

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


# Per-coordinate slack when comparing donor and shell outlines: both round
# fractional source coordinates, but not always to the same integer.
_OUTLINE_TOLERANCE = 1
# Distance allowed between two contours that do not match point by point:
# each side may round a point one step off, in opposite directions.
_OUTLINE_DISTANCE_TOLERANCE = 2 * _OUTLINE_TOLERANCE
# Longest flattening step for curves in the geometric comparison.
_FLATTEN_STEP = 10


def _outlines_match(a, b, tolerance=_OUTLINE_TOLERANCE):
    """True if two charstrings draw the same outline, hints ignored.

    Used to check that the makeotf donor drew the same glyph as the ufo2ft
    shell before the donor's charstring replaces the shell's. AFDKO ``tx``
    (4.x and 5.x, UFO reader) can drop the ``moveto`` of a contour when the
    glyph carries ``autohint.v2`` hints, merging two contours into one
    subpath; which glyphs it hits varies with the font and the UFO path.

    Both compiles draw the same UFO, but legitimately differ in encoding:

    - ``tx`` keeps fractional coordinates, ufo2ft rounds them, and the two do
      not always round to the same integer; a segment that rounds to zero
      length is dropped by ufo2ft and kept by ``tx``.
    - ``tx`` writes the closing ``lineto`` back to the start point explicitly.
    - ufo2ft drops the interior points of straight runs (also points a
      rounding step off the line, and spurs that run back along it) and writes
      a curve whose handles sit on its end points as a line; ``tx`` keeps
      both.
    - ufo2ft reverses mirrored components (negative scale) to keep the outline
      direction, ``tx`` does not -- the same contour then starts elsewhere and
      runs the other way.

    So the contours are normalized (see :func:`_outline_contours`) and matched
    one to one, each up to its start point and direction: first point by
    point (segment types equal, every point within ``tolerance`` per
    coordinate), and for the few contours left over -- where the two sides
    decided differently whether a point near a straight line is a corner --
    geometrically, every point of each contour within
    ``_OUTLINE_DISTANCE_TOLERANCE`` of the other. The contour count must
    agree exactly, which is what a merged contour breaks.

    Args:
        a: A fontTools ``T2CharString``.
        b: Another one.
        tolerance: Allowed difference per coordinate, in font units.

    Returns:
        True if the outlines match.
    """
    contours_a = _outline_contours(a, tolerance)
    contours_b = _outline_contours(b, tolerance)
    if len(contours_a) != len(contours_b):
        return False
    unused = list(contours_b)
    leftover = []
    for contour in contours_a:
        for i, other in enumerate(unused):
            if _contours_match(contour, other, tolerance):
                del unused[i]
                break
        else:
            leftover.append(contour)
    for contour in leftover:
        for i, other in enumerate(unused):
            if _contours_near(contour, other, _OUTLINE_DISTANCE_TOLERANCE):
                del unused[i]
                break
        else:
            return False
    return True


def _outline_contours(charstring, tolerance=0):
    """Closed contours of a charstring as normalized lists of segments.

    A segment is a tuple of points from its start on-curve point to its end
    on-curve point (2 points: line, 4: cubic). Coordinates are rounded with
    ``otRound``; curves whose handles sit on their end points become lines,
    segments no longer than ``tolerance`` per coordinate (including the
    closing one) are dropped -- one compile may round a tiny segment to zero
    and the other to one unit -- and straight runs whose interior points are
    within ``2 * tolerance`` of the line are joined.
    """
    from fontTools.misc.roundTools import otRound
    from fontTools.pens.recordingPen import RecordingPen

    pen = RecordingPen()
    charstring.draw(pen)

    def pt(p):
        return (otRound(p[0]), otRound(p[1]))

    def degenerate(seg):
        xs = [p[0] for p in seg]
        ys = [p[1] for p in seg]
        return max(xs) - min(xs) <= tolerance and max(ys) - min(ys) <= tolerance

    contours = []
    segments = []
    current = start = None

    def close():
        if current is not None and start is not None:
            if not degenerate((current, start)):
                segments.append((current, start))
        if segments:
            # Twice the slack: each compile may round the point a step off the line.
            contours.append(_merge_collinear_lines(segments, 2 * tolerance))

    for op, args in pen.value:
        if op == "moveTo":
            close()
            segments = []
            current = start = pt(args[0])
        elif op == "lineTo":
            seg = (current, pt(args[0]))
            if not degenerate(seg):
                segments.append(seg)
                current = seg[-1]
        elif op in ("curveTo", "qCurveTo"):
            seg = (current,) + tuple(pt(p) for p in args)
            if set(seg[1:-1]) <= {seg[0], seg[-1]}:
                # Handles retracted onto the end points: a straight line.
                seg = (seg[0], seg[-1])
            if not degenerate(seg):
                segments.append(seg)
                current = seg[-1]
        elif op in ("closePath", "endPath"):
            close()
            segments = []
            current = start = None
    close()
    return contours


def _merge_collinear_lines(segments, tolerance=0):
    """Join consecutive straight segments that lie on one line.

    ufo2ft drops the interior on-curve points of straight runs, ``tx`` keeps
    them -- including a point that is off the line by a rounding step, or a
    spur that overshoots and runs back along the same line. A point is
    interior when it is within ``tolerance`` of the line through its
    neighbours. Runs that wrap around the contour's start point are joined as
    well.
    """

    def continues(a, b):
        if len(a) != 2 or len(b) != 2:
            return False
        (x0, y0), (x1, y1) = a
        (x2, y2) = b[1]
        dx, dy = x2 - x0, y2 - y0
        if dx == 0 and dy == 0:
            return False
        cross = dx * (y1 - y0) - dy * (x1 - x0)
        return cross * cross <= tolerance * tolerance * (dx * dx + dy * dy)

    merged = []
    for seg in segments:
        if merged and continues(merged[-1], seg):
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)
    while len(merged) > 1 and continues(merged[-1], merged[0]):
        merged[0] = (merged[-1][0], merged[0][1])
        merged.pop()
    return merged


def _contours_match(a, b, tolerance):
    """True if contour ``b`` is ``a`` up to start point, direction and slack."""
    if len(a) != len(b):
        return False

    def near(p, q):
        return abs(p[0] - q[0]) <= tolerance and abs(p[1] - q[1]) <= tolerance

    reverse = [tuple(reversed(seg)) for seg in reversed(b)]
    n = len(a)
    for seq in (b, reverse):
        for shift in range(n):
            if all(
                len(a[k]) == len(seq[(k + shift) % n])
                and all(near(p, q) for p, q in zip(a[k], seq[(k + shift) % n]))
                for k in range(n)
            ):
                return True
    return False


def _flatten_contour(segments):
    """Polyline through a contour, curves split into steps of at most _FLATTEN_STEP."""
    import math

    points = []
    for seg in segments:
        if len(seg) == 2:
            points.append(seg[0])
            continue
        p0, p1, p2, p3 = seg
        hull = sum(math.dist(u, v) for u, v in zip(seg, seg[1:]))
        steps = max(2, math.ceil(hull / _FLATTEN_STEP))
        for k in range(steps):
            t = k / steps
            mt = 1 - t
            points.append(
                tuple(
                    mt**3 * p0[i] + 3 * mt * mt * t * p1[i] + 3 * mt * t * t * p2[i] + t**3 * p3[i]
                    for i in (0, 1)
                )
            )
    return points


def _contours_near(a, b, distance):
    """True if every point of each contour's polyline is within ``distance`` of the other."""
    poly_a = _flatten_contour(a)
    poly_b = _flatten_contour(b)
    return _polyline_within(poly_a, poly_b, distance) and _polyline_within(poly_b, poly_a, distance)


def _polyline_within(points, polyline, distance):
    """True if every point lies within ``distance`` of the closed ``polyline``."""
    limit = distance * distance
    edges = list(zip(polyline, polyline[1:] + polyline[:1]))
    # The polylines are dense (curve steps of _FLATTEN_STEP units), so testing
    # the vertices of one against the edges of the other is enough.
    for px, py in points:
        for (x0, y0), (x1, y1) in edges:
            dx, dy = x1 - x0, y1 - y0
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 == 0 else ((px - x0) * dx + (py - y0) * dy) / length2
            t = min(1.0, max(0.0, t))
            ex, ey = x0 + t * dx - px, y0 + t * dy - py
            if ex * ex + ey * ey <= limit:
                break
        else:
            return False
    return True


def _autohint_shell_glyphs(shell, glyph_names, temp_dir, logger=None):
    """Hint ``glyph_names`` in the shell in place with ``afdko.otfautohint``.

    The shell is saved as-is (its Private dict already holds the donor's blue
    zones and stem snaps), hinted with ``glyphList`` restricted to
    ``glyph_names``, and the resulting charstrings are copied back. Each glyph
    keeps the shell's own width operand. A failure is logged and leaves the
    glyphs unhinted -- their outlines are still correct.

    Args:
        shell: The shell ``TTFont`` (modified in place).
        glyph_names: Glyphs to hint.
        temp_dir: Scratch directory for the intermediate fonts.
        logger: Optional logger.

    Returns:
        Number of glyphs that came back hinted.
    """
    src = os.path.join(temp_dir, "rehint_in.otf")
    dst = os.path.join(temp_dir, "rehint_out.otf")
    try:
        from afdko.otfautohint.autohint import ACOptions, hintFiles

        shell.save(src)
        options = ACOptions()
        options.inputPaths = [src]
        options.outputPaths = [dst]
        options.allowNoBlues = True
        options.glyphList = list(glyph_names)
        options.explicitGlyphs = True
        hintFiles(options)

        rehinted_font = TTFont(dst)
        source_cs = rehinted_font["CFF "].cff.topDictIndex[0].CharStrings
        shell_cs = shell["CFF "].cff.topDictIndex[0].CharStrings
        count = 0
        for name in glyph_names:
            donor = source_cs[name]
            donor.decompile()
            if not any(isinstance(op, str) and op in HINT_OPS for op in donor.program):
                continue
            target = shell_cs[name]
            target.decompile()
            width, _ = _split_width_prefix(target.program)
            _, body = _split_width_prefix(donor.program)
            target.program = width + body
            count += 1
        rehinted_font.close()
    except Exception as e:
        if logger:
            logger.warning(f"Preserve: otfautohint failed on {glyph_names}, left unhinted: {e}")
        return 0
    if logger and count < len(glyph_names):
        logger.warning(
            f"Preserve: otfautohint hinted {count}/{len(glyph_names)} re-hinted glyph(s); "
            "the rest are unhinted"
        )
    return count


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
        shell = PostProcessor(shell, ufo).process(useProductionNames=None, optimizeCFF=subroutinize)
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


def _public_ps_to_v2_copy(ufo_path, temp_dir, logger=None):
    """Give ``tx`` the ``public.postscript.hints`` it cannot read.

    ``tx -t1`` reads only ``com.adobe.type.autohint`` (v1/v2) from the default
    layer, so a FontLab-style UFO whose hints live in ``public.postscript.hints``
    compiled with no hints at all. Every glyph that has public hints and no
    Adobe entry gets them converted to ``autohint.v2`` in a copy of the UFO
    (an Adobe entry wins when both exist); the source is never written.

    The conversion goes through the processedglyphs layer (point tags are
    remapped by position there), and that layer must not survive into the
    copy -- ``tx`` would read its decomposed outlines. A processedglyphs layer
    already in the source is set aside under another name and restored.

    Returns:
        ``(path, converted)``: the copy and the number of converted glyphs, or
        ``(ufo_path, 0)`` when no glyph needs converting.
    """
    from defcon import Font

    from ufo_tdkit_tools.constants import (
        ADOBE_HINT_KEY_V1,
        ADOBE_HINT_KEY_V2,
        PROCESSED_LAYER_NAME,
        PUBLIC_PS_HINT_KEY,
    )

    names = [
        glyph.name
        for glyph in Font(ufo_path)
        if glyph.lib.get(PUBLIC_PS_HINT_KEY)
        and ADOBE_HINT_KEY_V2 not in glyph.lib
        and ADOBE_HINT_KEY_V1 not in glyph.lib
    ]
    if not names:
        return ufo_path, 0

    import fontParts.world as fp

    from ufo_tdkit_tools.ps_hints.converter import export_from_processed, import_to_processed

    copy_path = os.path.join(temp_dir, os.path.basename(os.path.normpath(ufo_path)))
    shutil.copytree(ufo_path, copy_path)
    font = fp.OpenFont(copy_path, showInterface=False)

    stashed = None
    if PROCESSED_LAYER_NAME in [layer.name for layer in font.layers]:
        stashed = font.getLayer(PROCESSED_LAYER_NAME)
        stashed.name = PROCESSED_LAYER_NAME + ".preserve-stash"

    converted = 0
    for name in names:
        glyph = font[name]
        if import_to_processed(glyph, font, "public_ps") and export_from_processed(
            glyph, font, "v2"
        ):
            converted += 1

    if PROCESSED_LAYER_NAME in [layer.name for layer in font.layers]:
        font.removeLayer(PROCESSED_LAYER_NAME)
    if stashed is not None:
        stashed.name = PROCESSED_LAYER_NAME
    font.save()
    font.close()

    if logger:
        logger.info(
            f"Preserve: converted public.postscript.hints -> autohint.v2 for "
            f"{converted}/{len(names)} glyph(s) without Adobe hints"
        )
    return copy_path, converted


def compile_otf_preserve(
    ufo_path,
    otf_path,
    logger=None,
    pshash_rebuild=False,
    tx_path=None,
    makeotf_path=None,
    require_hints=False,
):
    """Compile OTF preserving PS hints from UFO.

    Delegates to compile_otf_preserve_optimized which uses the per-glyph
    charstring merge approach.

    Args:
        ufo_path: Path to UFO source with ``com.adobe.type.autohint.v2`` or
            ``public.postscript.hints`` hints
        otf_path: Path for output OTF
        logger: Optional logger
        pshash_rebuild: If True, rebuild processedglyphs layer before compilation
        tx_path: Absolute path to tx binary (for parallel workers)
        makeotf_path: Absolute path to makeotf binary (for parallel workers)
        require_hints: Fail instead of writing an unhinted OTF when the UFO
            has no hints at all

    Returns:
        True on success, False on failure

    Raises:
        FeatureCompilationError: the UFO's feature code does not compile.
    """
    return compile_otf_preserve_optimized(
        ufo_path,
        otf_path,
        logger,
        pshash_rebuild=pshash_rebuild,
        tx_path=tx_path,
        makeotf_path=makeotf_path,
        require_hints=require_hints,
    )


def compile_otf_preserve_optimized(
    ufo_path,
    otf_path,
    logger=None,
    pshash_rebuild=False,
    tx_path=None,
    makeotf_path=None,
    stats=None,
    require_hints=False,
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

    Glyphs hinted only in ``public.postscript.hints`` (FontLab) are converted
    to ``autohint.v2`` in a temporary copy before step 2, since ``tx`` reads
    only the Adobe keys; an Adobe entry wins when a glyph has both.

    A UFO with hints from which no hinted charstring comes out is a failure,
    not an unhinted OTF. A UFO with no hints at all still compiles unhinted
    unless ``require_hints`` is set.

    Args:
        ufo_path: Path to UFO source with ``com.adobe.type.autohint.v2`` or
            ``public.postscript.hints`` hints
        otf_path: Path for output OTF
        logger: Optional logger
        pshash_rebuild: If True, rebuild processedglyphs layer before makeotf
        tx_path: Absolute path to tx binary (for parallel workers)
        makeotf_path: Absolute path to makeotf binary (for parallel workers)
        stats: Optional mutable dict, filled with
               {'hints_transferred': int, 'total_glyphs': int,
                'donor_hinted': int, 'name_mismatch': int,
                'source_hinted': int, 'public_ps_converted': int,
                'outline_mismatch': int, 'outline_rehinted': int}.
               ``hints_transferred`` counts donor charstrings merged into
               the shell; ``outline_mismatch`` the hinted donor glyphs
               rejected because their outline differs from the shell's (the
               shell outline is kept), ``outline_rehinted`` how many of those
               were then hinted by otfautohint.
        require_hints: Fail instead of writing an unhinted OTF when the UFO
            has no hints at all

    Returns:
        True on success, False on failure

    Raises:
        FeatureCompilationError: the UFO's feature code does not compile.
    """
    PRIVATE_HINT_ATTRS = [
        "BlueValues",
        "OtherBlues",
        "FamilyBlues",
        "FamilyOtherBlues",
        "BlueScale",
        "BlueShift",
        "BlueFuzz",
        "StdHW",
        "StdVW",
        "StemSnapH",
        "StemSnapV",
        "ForceBold",
    ]

    temp_dir = tempfile.mkdtemp(prefix="tdkit_preserve_opt_")
    shell_path = os.path.join(temp_dir, "shell.otf")
    hinted_path = os.path.join(temp_dir, "hinted.otf")

    try:
        # Step 1: Compile ufo2ft shell
        if logger:
            logger.info("Preserve: compiling ufo2ft shell")
        shell_ufo = _compile_ufo2ft_shell(ufo_path, shell_path, logger)
        if shell_ufo is None:
            return False

        # Step 1.5: Validate hint data, after making public.postscript.hints
        # readable for tx -- the report then describes what tx will see.
        hinted_ufo_path, public_converted = _public_ps_to_v2_copy(ufo_path, temp_dir, logger)

        from ufo_tdkit_tools.ps_hints.validator import validate_ps_hints

        hint_report = validate_ps_hints(hinted_ufo_path, logger)
        if not hint_report["valid"]:
            if logger:
                logger.warning(
                    f"PS hint validation found {len(hint_report['errors'])} error(s) -- "
                    "compilation will continue but some hints may be lost"
                )
                for err in hint_report["errors"][:10]:
                    logger.error(f"  Hint error in '{err['glyph']}': {err['message']}")
                if len(hint_report["errors"]) > 10:
                    logger.error(f"  ... and {len(hint_report['errors']) - 10} more error(s)")
        if hint_report["warnings"] and logger:
            for warn in hint_report["warnings"][:5]:
                logger.warning(f"  Hint warning in '{warn['glyph']}': {warn['message']}")
            if len(hint_report["warnings"]) > 5:
                logger.warning(f"  ... and {len(hint_report['warnings']) - 5} more warning(s)")

        # Note: no processedglyphs layer is built here. tx -t1 reads hints
        # directly from com.adobe.type.autohint.v2 in the default layer's glyph.lib.
        # Creating a processedglyphs layer would cause tx to read decomposed/overlap-removed
        # glyphs from that layer, where the hint point references no longer match the
        # modified outlines, resulting in lost hints.

        # Step 2: Compile makeotf without subroutinization
        if logger:
            logger.info("Preserve: compiling makeotf (no subroutinization)")
        makeotf_args = ["-nS", "-f", hinted_ufo_path, "-o", hinted_path]
        if not _compile_makeotf_hinted(
            hinted_ufo_path,
            hinted_path,
            makeotf_args,
            logger,
            tx_path=tx_path,
            makeotf_path=makeotf_path,
        ):
            if logger:
                logger.error("makeotf compilation failed in preserve mode")
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
                f"Preserve: {len(missing)}/{len(shell_names)} shell glyphs "
                f"have no counterpart in the makeotf donor "
                f"({coverage:.1%} name coverage); their hints cannot be merged. "
                f"Sample: {missing[:10]}"
            )

        transferred = 0
        donor_hinted = 0
        outline_mismatch = []
        for glyph_name in shell_names:
            if glyph_name not in hinted_cs:
                continue
            hinted_charstring = hinted_cs[glyph_name]
            hinted_charstring.decompile()
            has_hints = any(
                isinstance(op, str) and op in HINT_OPS for op in hinted_charstring.program
            )
            if has_hints:
                donor_hinted += 1
                shell_charstring = shell_cs[glyph_name]
                shell_charstring.decompile()
                # The donor must draw the same outline as the shell. AFDKO tx
                # can merge two contours into one subpath (dropped moveto);
                # the shell is exact, so keep it and re-hint it below.
                if not _outlines_match(hinted_charstring, shell_charstring):
                    outline_mismatch.append(glyph_name)
                    continue
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

        # Step 4.5: hint the glyphs whose donor outline was rejected, now
        # that the shell carries the donor's blue zones and stem snaps.
        rehinted = 0
        if outline_mismatch:
            if logger:
                logger.warning(
                    f"Preserve: {len(outline_mismatch)} glyph(s) came out of tx/makeotf "
                    "with an outline that differs from the UFO (likely the AFDKO tx "
                    "contour-merge bug); kept the ufo2ft outline and re-hinting them "
                    f"with otfautohint: {outline_mismatch}"
                )
            rehinted = _autohint_shell_glyphs(shell, outline_mismatch, temp_dir, logger)

        # Hinted donor glyphs the shell does not know about — the mirror image
        # of the name-coverage warning above, and the symptom of a namespace
        # mismatch that would otherwise pass as "this font has few hints".
        dropped = 0
        for glyph_name in hinted_cs.keys():
            if glyph_name in shell_cs:
                continue
            charstring = hinted_cs[glyph_name]
            charstring.decompile()
            if any(isinstance(op, str) and op in HINT_OPS for op in charstring.program):
                dropped += 1
        if dropped and logger:
            logger.warning(
                f"Preserve: {dropped} hinted donor glyph(s) have no shell "
                "counterpart and were dropped"
            )

        hinted.close()

        if stats is not None:
            stats["hints_transferred"] = transferred
            stats["total_glyphs"] = len(shell_names)
            stats["donor_hinted"] = donor_hinted + dropped
            stats["name_mismatch"] = len(missing)
            stats["source_hinted"] = hint_report["glyphs_with_hints"]
            stats["public_ps_converted"] = public_converted
            stats["outline_mismatch"] = len(outline_mismatch)
            stats["outline_rehinted"] = rehinted

        if logger:
            logger.info(
                f"Preserve: transferred {transferred} hinted charstrings "
                f"({transferred}/{len(shell_names)} glyphs)"
            )

        if transferred + rehinted == 0:
            if hint_report["glyphs_with_hints"]:
                # The UFO has hints and none reached the binary: shipping the
                # unhinted shell would lose them silently.
                if logger:
                    logger.error(
                        f"Preserve: {hint_report['glyphs_with_hints']} glyph(s) have hints "
                        "in the UFO but no hinted charstring came out of makeotf -- "
                        "refusing to write an unhinted OTF"
                    )
                return False
            if require_hints:
                if logger:
                    logger.error(
                        "Preserve: no hints found in UFO (com.adobe.type.autohint.v2 or "
                        "public.postscript.hints) and require_hints is set"
                    )
                return False
            if logger:
                logger.warning(
                    "No hinted glyphs found in UFO -- preserve mode has nothing to preserve. "
                    "Using standard ufo2ft compilation instead."
                )
            # Shell OTF is already a valid unhinted font -- it still needs its
            # production glyph names, which were deferred for the merge.
            return _finalize_shell(shell, shell_ufo, otf_path, subroutinize=False, logger=logger)

        # Step 5: Production glyph names + cffsubr subroutinization for ~38%
        # smaller CFF. Hints are preserved inside subroutines (callsubr/callgsubr).
        if logger:
            logger.info("Preserve: applying production names and subroutinization")
        saved = _finalize_shell(shell, shell_ufo, otf_path, subroutinize=True, logger=logger)

        if logger and saved:
            logger.info(f"Preserve: saved hybrid OTF: {otf_path}")
        return saved

    except FeatureCompilationError:
        # Not a preserve-mode problem and not recoverable here: the caller
        # must fail the face instead of falling back to another compiler.
        raise
    except Exception as e:
        if logger:
            logger.error(f"Preserve mode failed: {e}")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
