# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Command-line interface for ufo-tdkit-tools.

Exposes the :func:`ufo_tdkit_tools.pipeline.process_font` pipeline
(``optimize-otf``) and the Glyphs.app importer (``glyphs2ufo``) as a console
script. Designed for build logs: every run prints a single machine-parseable
summary line (``optimized=N autohinted=M failed=K``) and returns a non-zero exit
code when any input failed.

Entry points:

- console script ``ufo-tdkit-tools`` (see ``[project.scripts]`` in pyproject)
- ``python -m ufo_tdkit_tools`` (see ``__main__.py``)
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from ufo_tdkit_tools import __version__

# Suffixes process_font treats as binary fonts.
_BINARY_SUFFIXES = frozenset({".otf", ".ttf", ".woff", ".woff2"})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ufo-tdkit-tools",
        description="PostScript hint extraction, optimization and preserve-mode "
        "compilation for UFO fonts.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    opt = sub.add_parser(
        "optimize-otf",
        help="Re-hint/optimize OTF (or any binary/UFO) inputs through the pipeline.",
        description="Run each input through the full pipeline (extract -> "
        "optional optimize -> autohint if needed -> compile) and write a hinted "
        "OTF. Prints a 'optimized=N autohinted=M failed=K' summary and exits "
        "non-zero if any input failed.",
    )
    opt.add_argument("files", nargs="+", metavar="FILE", help="Input .otf/.ttf/.woff/.woff2/.ufo")
    out = opt.add_mutually_exclusive_group(required=True)
    out.add_argument(
        "--in-place",
        action="store_true",
        help="Rewrite each input .otf in place (a temporary UFO is created and "
        "discarded internally).",
    )
    out.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="Write '<stem>.otf' and '<stem>.ufo' for each input into DIR.",
    )
    opt.add_argument(
        "--optimize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the ps_hints optimizer before compiling (default: enabled).",
    )
    opt.add_argument(
        "--hint-source",
        choices=("auto", "processed", "v2", "public_ps"),
        default="auto",
        help="Hint source for UFO inputs (ignored for binary inputs). Default: auto.",
    )
    opt.add_argument(
        "--autohint",
        choices=("fill", "all", "off"),
        default="fill",
        help="What to do with glyphs the hint source does not cover: 'fill' "
        "autohints just those (default), 'all' re-hints every glyph and ignores "
        "authored hints, 'off' leaves them unhinted.",
    )
    opt.add_argument(
        "--keep-ufo",
        action="store_true",
        help="With --in-place, also write '<input>.ufo' next to each input "
        "instead of discarding it.",
    )
    verbosity = opt.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="Log per-file pipeline detail."
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="Only print the final summary line."
    )
    opt.set_defaults(func=_cmd_optimize_otf)

    g2u = sub.add_parser(
        "glyphs2ufo",
        help="Convert Glyphs.app sources (.glyphs/.glyphspackage) to UFO masters.",
        description="Convert each source to UFO masters, plus a .designspace when it "
        "has more than one master, repairing what glyphsLib would refuse (see "
        "docs/GLYPHS_IMPORT.md). Needs the 'glyphs' extra. Prints a "
        "'converted=N failed=K' summary and exits non-zero if any source failed.",
    )
    g2u.add_argument(
        "sources", nargs="+", metavar="SOURCE", help="Input .glyphs file or .glyphspackage"
    )
    g2u.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="Write into DIR (one source) or DIR/<source stem> (several). "
        "Default: a folder named after each source, beside it.",
    )
    g2u.add_argument(
        "--force",
        action="store_true",
        help="Replace UFOs / .designspace already at the destination. Without it a "
        "source whose outputs exist is skipped and counted as failed.",
    )
    corners = g2u.add_mutually_exclusive_group()
    corners.add_argument(
        "--keep-corners",
        dest="apply_corners",
        action="store_false",
        help="Leave corner/cap components as components instead of baking them "
        "into the outlines. The UFO then draws without them and does not build "
        "with process_font (glyphsLib's corner filter rejects defcon fonts).",
    )
    corners.add_argument(
        "--apply-corners",
        dest="apply_corners",
        action="store_true",
        help="Bake corner/cap components into the outlines (the default; kept "
        "for scripts written against 0.4.0).",
    )
    g2u.set_defaults(apply_corners=True)
    g2u.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what each source holds and the paths it would write; convert nothing.",
    )
    g2u_verbosity = g2u.add_mutually_exclusive_group()
    g2u_verbosity.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log progress and example glyphs for each warning group.",
    )
    g2u_verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="Only print the final summary line."
    )
    g2u.set_defaults(func=_cmd_glyphs2ufo)

    return parser


def _cmd_optimize_otf(args: argparse.Namespace) -> int:
    from ufo_tdkit_tools.pipeline import process_font

    log = logging.getLogger("ufo_tdkit_tools.cli")

    output_dir: Path | None = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    optimized = autohinted = failed = 0

    for raw in args.files:
        src = Path(raw)
        if not src.exists():
            log.error(f"{src}: not found")
            failed += 1
            continue

        suffix = src.suffix.lower()
        is_binary = suffix in _BINARY_SUFFIXES

        if args.in_place and is_binary and suffix != ".otf":
            log.error(f"{src}: --in-place only supports .otf inputs (got {suffix})")
            failed += 1
            continue

        try:
            result = _process_one(
                process_font,
                src,
                in_place=args.in_place,
                output_dir=output_dir,
                keep_ufo=args.keep_ufo,
                optimize=args.optimize,
                hint_source=args.hint_source,
                autohint=args.autohint,
                log=log,
            )
        except Exception as exc:  # noqa: BLE001 -- never let one file abort the batch
            log.error(f"{src}: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        if not result.success:
            log.error(f"{src}: {result.error}")
            failed += 1
            continue

        optimized += 1
        if result.autohinted:
            autohinted += 1
        if not args.quiet:
            extra = f" autohinted={result.autohinted_count}" if result.autohinted else ""
            opt_note = f" optimized={result.optimized_count}" if result.optimized else ""
            log.info(
                f"{src}: ok ({result.glyphs_with_hints}/{result.glyphs_total} hinted"
                f"{opt_note}{extra} -> otf "
                f"{result.otf_glyphs_hinted}/{result.otf_glyphs_total})"
            )

    # Single machine-parseable summary line on stdout.
    print(f"optimized={optimized} autohinted={autohinted} failed={failed}")
    return 1 if failed else 0


def _process_one(
    process_font,
    src: Path,
    *,
    in_place: bool,
    output_dir: Path | None,
    keep_ufo: bool,
    optimize: bool,
    hint_source: str,
    autohint: str,
    log: logging.Logger,
):
    """Run one input through process_font, placing outputs per the chosen mode.

    For ``--in-place`` the OTF is built in a temp dir and atomically moved over
    the original only on success, so a mid-pipeline failure never corrupts the
    source file.
    """
    if in_place:
        # Build on the same filesystem as the source: os.replace() below is only
        # atomic within one device, and a default /tmp tempdir may live on a
        # different filesystem (EXDEV: Invalid cross-device link).
        with tempfile.TemporaryDirectory(prefix=".ufo_tdkit_cli_", dir=src.parent) as tmp:
            tmp_otf = Path(tmp) / "out.otf"
            tmp_ufo = Path(tmp) / "out.ufo"
            result = process_font(
                src,
                tmp_otf,
                tmp_ufo,
                hint_source=hint_source,
                autohint=autohint,
                optimize=optimize,
                logger_=log,
            )
            if result.success:
                os.replace(tmp_otf, src)
                if keep_ufo:
                    dst_ufo = src.with_suffix(".ufo")
                    if dst_ufo.exists():
                        shutil.rmtree(dst_ufo)
                    shutil.move(str(tmp_ufo), str(dst_ufo))
            return result

    # output-dir mode
    out_otf = output_dir / f"{src.stem}.otf"
    out_ufo = output_dir / f"{src.stem}.ufo"
    return process_font(
        src,
        out_otf,
        out_ufo,
        hint_source=hint_source,
        autohint=autohint,
        optimize=optimize,
        logger_=log,
    )


def _cmd_glyphs2ufo(args: argparse.Namespace) -> int:
    log = logging.getLogger("ufo_tdkit_tools.cli")
    try:
        import glyphsLib  # noqa: F401
    except ImportError:
        log.error("glyphs2ufo needs glyphsLib: pip install 'ufo-tdkit-tools[glyphs]'")
        print("converted=0 failed=" + str(len(args.sources)))
        return 1

    from ufo_tdkit_tools.glyphs import converter as gconv
    from ufo_tdkit_tools.glyphs.fast_inspect import read_source
    from ufo_tdkit_tools.glyphs.warning_summary import summarize_warnings

    # glyphsLib reports through its logger, often thousands of lines per
    # source. The converter's own handler on that logger still collects them
    # into the grouped report below; only the raw echo to stderr is cut.
    logging.getLogger("glyphsLib").propagate = False

    converted = failed = 0
    several = len(args.sources) > 1

    for raw in args.sources:
        src = Path(raw)
        if not gconv.is_glyphs_source(src):
            log.error(f"{src}: not a .glyphs / .glyphspackage source")
            failed += 1
            continue
        if not src.exists():
            log.error(f"{src}: not found")
            failed += 1
            continue

        if args.output_dir:
            out_dir = Path(args.output_dir)
            if several:
                out_dir = out_dir / src.stem
        else:
            out_dir = gconv.default_output_dir(src)

        try:
            info, gs_font = read_source(src)
            planned = [out_dir / name for name in _planned_filenames(gs_font, src)]
        except Exception as exc:  # noqa: BLE001 -- never let one source abort the batch
            log.error(f"{src}: cannot read: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        existing = gconv.describe_existing(planned)
        if args.dry_run:
            print(f"{src}: {gconv.format_source_summary(info)}")
            for path in planned:
                mark = " (exists)" if path in existing else ""
                print(f"  -> {path}{mark}")
            converted += 1
            continue
        if existing and not args.force:
            names = ", ".join(p.name for p in existing[:5])
            more = f" (+{len(existing) - 5} more)" if len(existing) > 5 else ""
            log.error(
                f"{src}: {len(existing)} output(s) already exist in {out_dir}: "
                f"{names}{more}; use --force to replace them"
            )
            failed += 1
            continue

        try:
            result = _convert_interruptibly(gconv, src, out_dir, args, log)
        except gconv.ConversionCancelled as exc:
            log.error(f"{src}: cancelled; removed {len(exc.removed)} path(s) it had written")
            if existing:
                log.error(
                    "  outputs being replaced were deleted before the cancel and cannot be restored"
                )
            print(f"converted={converted} failed={failed + 1} cancelled=1")
            return 130
        except Exception as exc:  # noqa: BLE001
            log.error(f"{src}: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        converted += 1
        if not args.quiet:
            _report_conversion(log, src, result, summarize_warnings(result.warnings), args)

    counted = "planned" if args.dry_run else "converted"
    print(f"{counted}={converted} failed={failed}")
    return 1 if failed else 0


def _planned_filenames(gs_font, source: Path) -> list[str]:
    """UFO filenames plus the .designspace a conversion would write.

    The cheap counterpart of ``converter.plan_output_paths`` (which builds the
    whole designspace), using the same rule as Font-Rover's import dialog: a
    .designspace named after the source whenever there is more than one
    master. A single-master source with brace layers also gets one; that case
    is not predicted here.
    """
    from ufo_tdkit_tools.glyphs.fast_inspect import plan_output_filenames

    names = plan_output_filenames(gs_font)
    if len(names) > 1:
        names = names + [f"{source.stem}.designspace"]
    return names


def _convert_interruptibly(gconv, src: Path, out_dir: Path, args, log):
    """Run the conversion in a worker thread so Ctrl-C becomes a clean cancel.

    The converter polls the event between phases and removes what it wrote;
    a KeyboardInterrupt raised inside it would leave half-written UFOs behind.
    """
    import threading

    cancel = threading.Event()
    done = threading.Event()
    outcome: dict = {}

    def progress(step: int, total: int, message: str) -> None:
        if args.verbose:
            log.info(f"  [{step}/{total}] {message}" if total > 0 else f"  {message}")

    def work() -> None:
        try:
            outcome["result"] = gconv.convert_glyphs_to_ufos(
                src,
                out_dir,
                progress_callback=progress,
                apply_corners=args.apply_corners,
                cancel=cancel,
            )
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the caller
            outcome["error"] = exc
        finally:
            done.set()

    # Wait on our own event, not Thread.join/is_alive: before Python 3.13 a
    # KeyboardInterrupt inside join() corrupts the thread's state and
    # is_alive() then reports False while the worker is still running.
    worker = threading.Thread(target=work, name=f"glyphs2ufo:{src.name}", daemon=True)
    worker.start()
    while not done.is_set():
        try:
            done.wait(0.2)
        except KeyboardInterrupt:
            if cancel.is_set():
                raise
            log.error(f"{src}: cancelling (finishing the current phase; Ctrl-C again to abort)")
            cancel.set()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _report_conversion(log, src: Path, result, groups, args) -> None:
    log.warning(
        f"{src}: ok -> {result.open_path} ({result.master_count} master(s), "
        f"{result.glyph_count} glyphs, ps hints {result.ps_hints_imported}, "
        f"corners {result.corners_applied}, tt deltas {result.tt_deltas_restored})"
    )
    for group in groups:
        kind = "repair" if group.is_repair else group.category
        first, *rest = group.template.splitlines() or [""]
        log.warning(f"  {group.count:>6}  [{kind}] {first}{' ...' if rest else ''}")
        if args.verbose and group.examples:
            shown = ", ".join(group.examples[:10])
            more = f" (+{len(group.examples) - 10} more)" if len(group.examples) > 10 else ""
            log.warning(f"          e.g. {shown}{more}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    level = logging.WARNING
    if getattr(args, "verbose", False):
        level = logging.INFO
    elif getattr(args, "quiet", False):
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)

    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
