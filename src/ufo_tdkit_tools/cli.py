# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""Command-line interface for ufo-tdkit-tools.

Exposes the :func:`ufo_tdkit_tools.pipeline.process_font` pipeline as a console
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
            extra = " autohinted" if result.autohinted else ""
            opt_note = f" optimized={result.optimized_count}" if result.optimized else ""
            log.info(
                f"{src}: ok ({result.glyphs_with_hints}/{result.glyphs_total} hinted"
                f"{opt_note}{extra})"
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
        optimize=optimize,
        logger_=log,
    )


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
